"""Reconstructing volume(s) from picked cryoEM and cryoET particles."""

import os
import shutil
import pickle
import logging

from datetime import datetime as dt
import numpy as np
import time

import torch
import torch.nn.functional as F
from torch.utils.data import DataLoader
from torch.utils.tensorboard import SummaryWriter

from . import mrc
from . import utils
from . import dataset_fast
from . import dataset
from . import ctf
from . import summary

from .configuration import TrainingConfigurations
from .lattice import Lattice
from .losses import kl_divergence_conf,  l1_regularizer, l2_frequency_bias
from .models import CryoDRGN3, MyDataParallel
from .mask import CircularMask, FrequencyMarchingMask


class ModelTrainer:
    """An engine for training the reconstruction model on particle data.

    Attributes
    ----------
    configs (TrainingConfigurations):   Values of all parameters that can be
                                        set by the user.

    n_particles_dataset (int):  The number of picked particles in the data.
    pretraining (bool):     Whether we are in the pretraining stage.
    epoch (int):    Which training epoch the model is in.

    logger (logging.Logger):    Utility for printing and writing information
                                about the model as it is running.
    """

    # options for optimizers to use
    optim_types = {'adam': torch.optim.Adam, 'lbfgs': torch.optim.LBFGS}

    # placeholders for runtimes
    run_phases = [
        'dataloading',
        'to_gpu',
        'ctf',
        'encoder',
        'decoder',
        'decoder_coords',
        'decoder_query',
        'loss',
        'backward',
        'to_cpu'
        ]

    def __init__(self, config_vals: dict) -> None:
        self.logger = logging.getLogger(__name__)
        self.configs = TrainingConfigurations(config_vals)

        # output directory
        if os.path.exists(self.configs.outdir) and os.listdir(self.configs.outdir):
            self.logger.warning("Output directory already exists."
                                "Renaming the old one.")

            newdir = self.configs.outdir + '_old'
            if os.path.exists(newdir):
                self.logger.warning("Must delete the previously "
                                    "saved old output.")
                shutil.rmtree(newdir)

            os.rename(self.configs.outdir, newdir)

        os.makedirs(self.configs.outdir, exist_ok=True)
        self.logger.addHandler(logging.FileHandler(
            os.path.join(self.configs.outdir, "training.log")))

        n_gpus = torch.cuda.device_count()
        self.logger.info(f"Number of available gpus: {n_gpus}")
        self.n_prcs = max(n_gpus, 1)

        self.batch_size_known_poses = (
                self.configs.batch_size_known_poses * self.n_prcs)
        self.batch_size_hps = self.configs.batch_size_hps * self.n_prcs
        self.batch_size_sgd = self.configs.batch_size_sgd * self.n_prcs

        np.random.seed(self.configs.seed)
        torch.manual_seed(self.configs.seed)

        # set the device
        self.use_cuda = torch.cuda.is_available()
        self.device = torch.device('cuda:0' if self.use_cuda else 'cpu')
        self.logger.info(f"Use cuda {self.use_cuda}")

        # tensorboard writer
        self.summaries_dir = os.path.join(self.configs.outdir, 'summaries')
        os.makedirs(self.summaries_dir, exist_ok=True)
        self.writer = SummaryWriter(self.summaries_dir)
        self.logger.info("Will write tensorboard summaries "
                         f"in {self.summaries_dir}")

        # load the optional index used to filter particles
        if self.configs.ind is not None:
            if isinstance(self.configs.ind, int):
                self.logger.info(f"Keeping {self.configs.ind} particles")
                self.index = np.arange(self.configs.ind)

            elif isinstance(self.configs.ind, str):
                if not os.path.exists(self.configs.ind):
                    raise ValueError("Given subset index file "
                                     f"`{self.configs.ind}` does not exist!")

                self.logger.info(
                    f"Filtering dataset with {self.configs.ind}")
                self.index = pickle.load(open(self.configs.ind, 'rb'))

        else:
            self.index = None

        # load the particles
        self.logger.info("Creating dataset")
        if self.configs.fast_dataloading:
            if not self.configs.subtomogram_averaging:
                self.data = dataset_fast.ImageDataset(
                    self.configs.particles,
                    max_threads=self.configs.max_threads,
                    lazy=True, poses_gt_pkl=self.configs.pose,
                    resolution_input=self.configs.resolution_encoder,
                    window_r=self.configs.window_radius_gt_real,
                    datadir=self.configs.datadir,
                    no_trans=self.configs.no_trans
                    )

            else:
                self.data = dataset_fast.TiltSeriesData(
                    self.configs.particles, self.configs.n_tilts, self.configs.angle_per_tilt,
                    window_r=self.configs.window_radius_gt_real,
                    datadir=self.configs.datadir,
                    max_threads=self.configs.max_threads,
                    dose_per_tilt=self.configs.dose_per_tilt,
                    device=self.device,
                    poses_gt_pkl=self.configs.pose,
                    tilt_axis_angle=self.configs.tilt_axis_angle,
                    ind=self.index, no_trans=self.configs.no_trans
                    )

        else:
            self.data = dataset.MRCData(
                self.configs.particles, max_threads=self.configs.max_threads,
                ind=self.index, lazy=self.configs.lazy,
                relion31=self.configs.relion31, poses_gt_pkl=self.configs.pose,
                resolution_input=self.configs.resolution_encoder,
                window_r=self.configs.window_radius_gt_real,
                datadir=self.configs.datadir, no_trans=self.configs.no_trans
                )

        self.n_particles_dataset = self.data.N
        self.n_tilts_dataset = (self.data.Nt
                                if self.configs.subtomogram_averaging
                                else self.data.N)
        self.resolution = self.data.D

        # load ctf
        if self.configs.ctf is not None:
            self.logger.info(f"Loading ctf params from {self.configs.ctf}")

            ctf_params = ctf.load_ctf_for_training(self.resolution - 1,
                                                   self.configs.ctf)

            if self.index is not None:
                self.logger.info("Filtering dataset")
                ctf_params = ctf_params[self.index]

            assert ctf_params.shape == (self.n_tilts_dataset, 8)
            if self.configs.subtomogram_averaging:
                ctf_params = np.concatenate(
                    (ctf_params, self.data.ctfscalefactor.reshape(-1, 1)),
                    axis=1  # type: ignore
                    )
                self.data.voltage = float(ctf_params[0, 4])

            self.ctf_params = torch.tensor(ctf_params)
            self.ctf_params = self.ctf_params.to(self.device)

            if self.configs.subtomogram_averaging:
                self.data.voltage = float(self.ctf_params[0, 4])

        else:
            self.ctf_params = None

        # lattice
        self.logger.info("Building lattice")
        self.lattice = Lattice(self.resolution, extent=0.5, device=self.device)

        # output mask
        if self.configs.output_mask == 'circ':
            radius = (self.lattice.D // 2 if self.configs.max_freq is None
                      else self.configs.max_freq)
            self.output_mask = CircularMask(self.lattice, radius)

        elif self.configs.output_mask == 'frequency_marching':
            self.output_mask = FrequencyMarchingMask(
                self.lattice, self.lattice.D // 2,
                radius=self.configs.l_start_fm,
                add_one_every=self.configs.add_one_frequency_every
                )

        else:
            raise NotImplementedError

        # pose search
        ps_params = None
        self.epochs_pose_search = 0

        if self.configs.n_imgs_pose_search > 0:
            self.epochs_pose_search = max(2,
                                          self.configs.n_imgs_pose_search
                                          // self.n_particles_dataset + 1)

        if self.epochs_pose_search > 0:
            ps_params = {
                'l_min': self.configs.l_start,
                'l_max': self.configs.l_end,
                't_extent': self.configs.t_extent,
                't_n_grid': self.configs.t_n_grid,
                'niter': self.configs.n_iter,
                'nkeptposes': self.configs.n_kept_poses,
                'base_healpy': self.configs.base_healpy,
                't_xshift': self.configs.t_x_shift,
                't_yshift': self.configs.t_y_shift,
                'no_trans_search_at_pose_search': self\
                    .configs.no_trans_search_at_pose_search,
                'n_tilts_pose_search': self.configs.n_tilts_pose_search,
                'tilting_func': (self.data.get_tilting_func()
                                 if self.configs.subtomogram_averaging
                                 else None),
                'average_over_tilts': self.configs.average_over_tilts
                }

        # cnn
        cnn_params = {
            'conf': self.configs.use_conf_encoder,
            'depth_cnn': self.configs.depth_cnn,
            'channels_cnn': self.configs.channels_cnn,
            'kernel_size_cnn': self.configs.kernel_size_cnn
            }

        # conformational encoder
        if self.configs.z_dim > 0:
            self.logger.info("Heterogeneous reconstruction with "
                             f"z_dim = {self.configs.z_dim}")
        else:
            self.logger.info("Homogeneous reconstruction")

        conf_regressor_params = {
            'z_dim': self.configs.z_dim,
            'std_z_init': self.configs.std_z_init,
            'variational': self.configs.variational_het
            }

        # hypervolume
        hyper_volume_params = {
            'explicit_volume': self.configs.explicit_volume,
            'n_layers': self.configs.hypervolume_layers,
            'hidden_dim': self.configs.hypervolume_dim,
            'pe_type': self.configs.pe_type,
            'pe_dim': self.configs.pe_dim,
            'feat_sigma': self.configs.feat_sigma,
            'domain': self.configs.hypervolume_domain,
            'extent': self.lattice.extent,
            'pe_type_conf': self.configs.pe_type_conf
            }

        will_use_point_estimates = self.configs.epochs_sgd >= 1
        self.logger.info("Initializing model...")

        self.model = CryoDRGN3(
            self.lattice,
            self.output_mask,
            self.n_particles_dataset,
            self.n_tilts_dataset,
            cnn_params,
            conf_regressor_params,
            hyper_volume_params,
            resolution_encoder=self.configs.resolution_encoder,
            no_trans=self.configs.no_trans,
            use_gt_poses=self.configs.use_gt_poses,
            use_gt_trans=self.configs.use_gt_trans,
            will_use_point_estimates=will_use_point_estimates,
            ps_params=ps_params,
            verbose_time=self.configs.verbose_time,
            pretrain_with_gt_poses=self.configs.pretrain_with_gt_poses,
            n_tilts_pose_search=self.configs.n_tilts_pose_search,
            n_classes=self.configs.n_classes
            )

        # TODO: auto-loading from last weights file if load=True?
        # initialization from a previous checkpoint
        if self.configs.load:
            self.logger.info(f"Loading checkpoint from {self.configs.load}")
            checkpoint = torch.load(self.configs.load)
            state_dict = checkpoint['model_state_dict']

            if 'base_shifts' in state_dict:
                state_dict.pop('base_shifts')

            self.logger.info(
                self.model.load_state_dict(state_dict, strict=False))
            self.start_epoch = checkpoint['epoch'] + 1

            if 'output_mask_radius' in checkpoint:
                self.output_mask.update_radius(
                    checkpoint['output_mask_radius'])

        else:
            self.start_epoch = -1

        # move to gpu and parallelize
        self.logger.info(self.model)
        parameter_count = sum(p.numel() for p in self.model.parameters()
                              if p.requires_grad)
        self.logger.info(f"{parameter_count} parameters in model")

        # TODO: Replace with DistributedDataParallel
        if self.n_prcs > 1:
            self.model = MyDataParallel(self.model)

        self.logger.info("Model initialized. Moving to GPU...")
        self.model.to(self.device)
        self.model.output_mask\
            .binary_mask = self.model.output_mask.binary_mask.cpu()

        self.optimizers = dict()
        self.optimizer_types = dict()

        # hypervolume
        hyper_volume_params = [{
            'params': list(self.model.hypervolume.parameters())}]

        self.optimizers['hypervolume'] = self.optim_types[
            self.configs.hypervolume_optimizer_type](hyper_volume_params,
                                                     lr=self.configs.lr)
        self.optimizer_types[
            'hypervolume'] = self.configs.hypervolume_optimizer_type

        # pose table
        if not self.configs.use_gt_poses:
            if self.configs.epochs_sgd > 0:
                pose_table_params = [{
                    'params': list(self.model.pose_table.parameters())}]

                self.optimizers['pose_table'] = self.optim_types[
                    self.configs.pose_table_optimizer_type](
                        pose_table_params, lr=self.configs.lr_pose_table)
                self.optimizer_types[
                    'pose_table'] = self.configs.pose_table_optimizer_type

        # conformations
        if self.configs.z_dim > 0:
            if self.configs.use_conf_encoder:
                conf_encoder_params = [{
                    'params': (list(self.model.conf_cnn.parameters())
                               + list(self.model.conf_regressor.parameters()))
                    }]

                self.optimizers['conf_encoder'] = self.optim_types[
                    self.configs.conf_encoder_optimizer_type](
                        conf_encoder_params, lr=self.configs.lr_conf_encoder,
                        weight_decay=self.configs.wd
                        )
                self.optimizer_types[
                    'conf_encoder'] = self.configs.conf_encoder_optimizer_type

            else:
                conf_table_params = [{
                    'params': list(self.model.conf_table.parameters())}]

                self.optimizers['conf_table'] = self.optim_types[
                    self.configs.conf_table_optimizer_type](
                        conf_table_params, lr=self.configs.lr_conf_table)

                self.optimizer_types[
                    'conf_table'] = self.configs.conf_table_optimizer_type

        # scores
        self.n_classes = self.configs.n_classes
        self.std_noise = self.configs.std_noise
        if self.n_classes > 1:
            score_table_params = [{'params': list(self.model.score_table.parameters())}]

            self.optimizers['score_table'] = self.optim_types[
                self.configs.score_table_optimizer_type
            ](score_table_params, lr=self.configs.lr_score_table)

            self.optimizer_types[
                'score_table'] = self.configs.score_table_optimizer_type

        self.optimized_modules = []

        # initialization from a previous checkpoint
        if self.configs.load:
            checkpoint = torch.load(self.configs.load)

            for key in self.optimizers:
                self.optimizers[key].load_state_dict(
                    checkpoint['optimizers_state_dict'][key])

        # dataloaders
        if self.configs.fast_dataloading:
            self.data_generator_pose_search = dataset_fast.make_dataloader(
                self.data, batch_size=self.batch_size_hps,
                num_workers=self.configs.num_workers,
                shuffler_size=self.configs.shuffler_size
                )
            self.data_generator = dataset_fast.make_dataloader(
                self.data, batch_size=self.batch_size_known_poses,
                num_workers=self.configs.num_workers,
                shuffler_size=self.configs.shuffler_size
                )
            self.data_generator_latent_optimization = dataset_fast\
                .make_dataloader(self.data, batch_size=self.batch_size_sgd,
                                 num_workers=self.configs.num_workers,
                                 shuffler_size=self.configs.shuffler_size)

        else:
            self.data_generator_pose_search = DataLoader(
                self.data, batch_size=self.batch_size_hps,
                shuffle=self.configs.shuffle,
                num_workers=self.configs.num_workers, drop_last=False
                )
            self.data_generator = DataLoader(
                self.data, batch_size=self.batch_size_known_poses,
                shuffle=self.configs.shuffle,
                num_workers=self.configs.num_workers, drop_last=False
                )
            self.data_generator_latent_optimization = DataLoader(
                self.data, batch_size=self.batch_size_sgd,
                shuffle=self.configs.shuffle,
                num_workers=self.configs.num_workers, drop_last=False
                )

        # save configurations
        self.configs.write(os.path.join(self.configs.outdir,
                                        'train-configs.yaml'))

        epsilon = 1e-8
        # booleans
        self.log_latents = False
        self.pose_only = True
        self.pretraining = False
        self.is_in_pose_search_step = False
        self.use_point_estimates = False
        self.first_switch_to_point_estimates = True
        self.first_switch_to_point_estimates_conf = True

        if self.configs.load is not None:
            if self.start_epoch >= self.epochs_pose_search:
                self.first_switch_to_point_estimates = False
            self.first_switch_to_point_estimates_conf = False

        self.use_kl_divergence = (not self.configs.z_dim == 0
                                  and self.configs.variational_het
                                  and self.configs.beta_conf >= epsilon)
        self.use_trans_l1_regularizer = (
                self.configs.trans_l1_regularizer >= epsilon
                and not self.configs.use_gt_trans and not self.configs.no_trans
                )
        self.use_l2_smoothness_regularizer = (
                self.configs.l2_smoothness_regularizer >= epsilon)

        self.num_epochs = self.epochs_pose_search + self.configs.epochs_sgd
        if self.configs.load:
            self.num_epochs += self.start_epoch

        self.n_particles_pretrain = (self.configs.n_imgs_pretrain
                                     if self.configs.n_imgs_pretrain >= 0
                                     else self.n_particles_dataset)

        # placeholders for predicted latent variables,
        # last input/output batch, losses
        self.in_dict_last = None
        self.y_pred_last = None

        self.predicted_rots = np.empty((self.n_tilts_dataset, 3, 3))
        self.predicted_trans = (np.empty((self.n_tilts_dataset, 2))
                                if not self.configs.no_trans else None)
        self.predicted_conf = (np.empty((self.n_particles_dataset,
                                         self.configs.z_dim))
                               if self.configs.z_dim > 0 else None)
        self.predicted_logvar = (
            np.empty((self.n_particles_dataset, self.configs.z_dim))
            if self.configs.z_dim > 0 and self.configs.variational_het
            else None
            )
        self.predicted_rots_all_classes = (np.empty((self.n_tilts_dataset, self.n_classes, 3, 3))
                                           if self.n_classes > 1 else None)
        self.predicted_trans_all_classes = (np.empty((self.n_tilts_dataset, self.n_classes, 2))
                                           if self.n_classes > 1 and not self.configs.no_trans else None)
        self.predicted_idx_best_class = (np.empty((self.n_particles_dataset))
                                         if self.n_classes > 1 else None)
        self.predicted_p_classes = (np.empty((self.n_particles_dataset, self.n_classes))
                                    if self.n_classes > 1 else None)

        self.mask_particles_seen_at_last_epoch = np.zeros(
            self.n_particles_dataset)
        self.mask_tilts_seen_at_last_epoch = np.zeros(self.n_tilts_dataset)

        # counters
        self.epoch = 0
        self.run_times = {phase: [] for phase in self.run_phases}
        self.current_epoch_particles_count = 0
        self.total_batch_count = 0
        self.total_particles_count = 0
        self.batch_idx = 0
        self.cur_loss = None

    def train(self):
        self.logger.info("--- Training Starts Now ---")
        t_0 = dt.now()

        self.predicted_rots = np.eye(3).reshape(1, 3, 3).repeat(
            self.n_tilts_dataset, axis=0)
        self.predicted_trans = (np.zeros((self.n_tilts_dataset, 2))
                                if not self.configs.no_trans else None)
        self.predicted_conf = (np.zeros((self.n_particles_dataset,
                                         self.configs.z_dim))
                               if self.configs.z_dim > 0 else None)

        self.total_batch_count = 0
        self.total_particles_count = 0

        self.epoch = self.start_epoch - 1
        for epoch in range(self.start_epoch, self.num_epochs):
            te = dt.now()

            self.mask_particles_seen_at_last_epoch = np.zeros(
                self.n_particles_dataset)
            self.mask_tilts_seen_at_last_epoch = np.zeros(self.n_tilts_dataset)

            self.epoch += 1
            self.current_epoch_particles_count = 0
            self.optimized_modules = ['hypervolume']

            self.pose_only = (self.total_particles_count
                              < self.configs.pose_only_phase
                              or self.configs.z_dim == 0 or epoch < 0)
            self.pretraining = self.epoch < 0

            if not self.configs.use_gt_poses:
                self.is_in_pose_search_step = (
                        0 <= epoch < self.epochs_pose_search)
                self.use_point_estimates = (
                        epoch >= max(0, self.epochs_pose_search))

            n_max_particles = self.n_particles_dataset
            data_generator = self.data_generator

            # pre-training
            if self.pretraining:
                n_max_particles = self.n_particles_pretrain
                self.logger.info(
                    f"Will pretrain on {n_max_particles} particles")

            # HPS
            elif self.is_in_pose_search_step:
                n_max_particles = self.n_particles_dataset
                self.logger.info(
                    f"Will use pose search on {n_max_particles} particles")
                data_generator = self.data_generator_pose_search

            # SGD
            elif self.use_point_estimates:
                if self.first_switch_to_point_estimates:
                    self.first_switch_to_point_estimates = False
                    self.logger.info("Switched to autodecoding poses")

                    if self.configs.refine_gt_poses:
                        self.logger.info(
                            "Initializing pose table from ground truth")

                        poses_gt = utils.load_pkl(self.configs.pose)
                        if poses_gt[0].ndim == 3:
                            # contains translations
                            rotmat_gt = torch.tensor(poses_gt[0]).float()
                            trans_gt = torch.tensor(poses_gt[1]).float()
                            trans_gt *= self.resolution

                            if self.index is not None:
                                rotmat_gt = rotmat_gt[self.index]
                                trans_gt = trans_gt[self.index]

                        else:
                            rotmat_gt = torch.tensor(poses_gt).float()
                            trans_gt = None

                            if self.index is not None:
                                rotmat_gt = rotmat_gt[self.index]

                        if self.n_classes > 1:
                            rotmat_gt = rotmat_gt[:, None, :, :].repeat(1, self.n_classes, 1, 1)
                            trans_gt = (trans_gt[:, None, :].repeat(1, self.n_classes, 1)
                                        if trans_gt is not None else None)

                        self.model.pose_table.initialize(rotmat_gt, trans_gt)

                    else:
                        self.logger.info("Initializing pose table from "
                                         "hierarchical pose search")
                        if self.n_classes == 1:
                            self.model.pose_table.initialize(self.predicted_rots,
                                                             self.predicted_trans)
                        else:
                            self.model.pose_table.initialize(self.predicted_rots_all_classes,
                                                             self.predicted_trans_all_classes)

                    self.model.to(self.device)

                self.logger.info("Will use latent optimization on "
                                 f"{self.n_particles_dataset} particles")

                data_generator = self.data_generator_latent_optimization
                self.optimized_modules.append('pose_table')

            # GT poses
            else:
                assert self.configs.use_gt_poses

            # conformations
            if not self.pose_only:
                if self.configs.use_conf_encoder:
                    self.optimized_modules.append('conf_encoder')

                else:
                    if self.first_switch_to_point_estimates_conf:
                        self.first_switch_to_point_estimates_conf = False

                        if self.configs.initial_conf is not None:
                            self.logger.info("Initializing conformation table "
                                             "from given z's")
                            self.model.conf_table.initialize(utils.load_pkl(
                                self.configs.initial_conf))

                        self.model.to(self.device)

                    self.optimized_modules.append('conf_table')

            # scores
            if self.n_classes > 1:
                self.optimized_modules.append('score_table')

            will_make_summary = (
                    (self.configs.log_heavy_interval
                     and epoch % self.configs.log_heavy_interval == 0)
                    or self.is_in_pose_search_step or self.pretraining
                    )
            self.log_latents = will_make_summary

            if will_make_summary:
                self.logger.info(
                    "Will make a full summary at the end of this epoch")

            for key in self.run_times.keys():
                self.run_times[key] = []

            end_time = time.time()
            self.cur_loss = 0

            # inner loop
            for batch_idx, in_dict in enumerate(data_generator):
                self.batch_idx = batch_idx

                # with torch.autograd.detect_anomaly():
                self.train_step(in_dict, end_time=end_time)
                if self.configs.verbose_time:
                    torch.cuda.synchronize()

                end_time = time.time()

                if self.current_epoch_particles_count > n_max_particles:
                    break

            total_loss = self.cur_loss / n_max_particles
            self.logger.info(f"# =====> SGD Epoch: {self.epoch} "
                             f"finished in {dt.now() - te}; "
                             f"total loss = {format(total_loss, '.6f')}")

            # image and pose summary
            if will_make_summary:
                self.make_heavy_summary()
                self.save_latents()
                self.save_volume()
                self.save_model()

            # update output mask -- epoch-based scaling
            if (hasattr(self.output_mask, 'update_epoch')
                    and self.use_point_estimates):
                self.output_mask.update_epoch(
                    self.configs.n_frequencies_per_epoch)

        t_total = dt.now() - t_0
        self.logger.info(
            f"Finished in {t_total} ({t_total / self.num_epochs} per epoch)")

    def get_ctfs_at(self, index):
        batch_size = len(index)
        ctf_params_local = (self.ctf_params[index]
                            if self.ctf_params is not None else None)

        if ctf_params_local is not None:
            freqs = self.lattice.freqs2d.unsqueeze(0).expand(
                batch_size, *self.lattice.freqs2d.shape) / ctf_params_local[:, 0].view(batch_size, 1, 1)

            ctf_local = ctf.compute_ctf(
                freqs, *torch.split(ctf_params_local[:, 1:], 1, 1)).view(
                    batch_size, self.resolution, self.resolution)

        else:
            ctf_local = None

        return ctf_local

    def train_step(self, in_dict, end_time):
        if self.configs.verbose_time:
            torch.cuda.synchronize()
            self.run_times['dataloading'].append(time.time() - end_time)

        # update output mask -- image-based scaling
        if hasattr(self.output_mask, 'update') and self.is_in_pose_search_step:
            self.output_mask.update(self.total_particles_count)

        if self.is_in_pose_search_step:
            self.model.ps_params['l_min'] = self.configs.l_start

            if self.configs.output_mask == 'circ':
                self.model.ps_params['l_max'] = self.configs.l_end
            else:
                self.model.ps_params['l_max'] = min(
                    self.output_mask.current_radius, self.configs.l_end)

        y_gt = in_dict['y']
        ind = in_dict['index']

        if not 'tilt_index' in in_dict:
            in_dict['tilt_index'] = in_dict['index']
        else:
            in_dict['tilt_index'] = in_dict['tilt_index'].reshape(-1)

        ind_tilt = in_dict['tilt_index']
        self.total_batch_count += 1
        batch_size = len(y_gt)
        self.total_particles_count += batch_size
        self.current_epoch_particles_count += batch_size

        # move to gpu
        if self.configs.verbose_time:
            torch.cuda.synchronize()
        start_time_gpu = time.time()

        for key in in_dict.keys():
            in_dict[key] = in_dict[key].to(self.device)
        if self.configs.verbose_time:
            torch.cuda.synchronize()
            self.run_times['to_gpu'].append(time.time() - start_time_gpu)

        # zero grad
        for key in self.optimized_modules:
            self.optimizers[key].zero_grad()

        # forward pass
        latent_variables_dict, y_pred, y_gt_processed = self.forward_pass(
            in_dict)

        if self.n_prcs > 1:
            self.model.module.is_in_pose_search_step = False
        else:
            self.model.is_in_pose_search_step = False

        # loss
        if self.configs.verbose_time:
            torch.cuda.synchronize()

        start_time_loss = time.time()
        total_loss, all_losses = self.loss(y_pred, y_gt_processed,
                                           latent_variables_dict)

        if self.configs.verbose_time:
            torch.cuda.synchronize()
            self.run_times['loss'].append(time.time() - start_time_loss)

        # backward pass
        if self.configs.verbose_time:
            torch.cuda.synchronize()
        start_time_backward = time.time()
        total_loss.backward()
        self.cur_loss += total_loss.item() * len(ind)

        for key in self.optimized_modules:
            if self.optimizer_types[key] == 'adam':
                self.optimizers[key].step()

            elif self.optimizer_types[key] == 'lbfgs':
                def closure():
                    self.optimizers[key].zero_grad()
                    _latent_variables_dict, _y_pred, _y_gt_processed = self.forward_pass(in_dict)
                    _loss, _ = self.loss(
                        _y_pred, _y_gt_processed, _latent_variables_dict
                    )
                    _loss.backward()
                    return _loss.item()
                self.optimizers[key].step(closure)

            else:
                raise NotImplementedError

        if self.configs.verbose_time:
            torch.cuda.synchronize()

            self.run_times['backward'].append(
                time.time() - start_time_backward)

        # detach
        if self.log_latents:
            self.in_dict_last = in_dict
            if self.n_classes == 1:
                self.y_pred_last = y_pred
            else:
                if y_pred.dim() == 4:
                    self.y_pred_last = y_pred[np.arange(batch_size), :, latent_variables_dict['idx_best_class']]
                elif y_pred.dim() == 3:
                    self.y_pred_last = y_pred[np.arange(batch_size), latent_variables_dict['idx_best_class']]

            if self.configs.verbose_time:
                torch.cuda.synchronize()

            start_time_cpu = time.time()
            (rot_pred, trans_pred, conf_pred, logvar_pred, rot_all_classes, trans_all_classes, idx_best_class,
             p_classes) = self.detach_latent_variables(latent_variables_dict)

            if self.configs.verbose_time:
                torch.cuda.synchronize()
                self.run_times['to_cpu'].append(time.time() - start_time_cpu)

            # log
            if self.use_cuda:
                ind = ind.cpu()
                ind_tilt = ind_tilt.cpu()

            self.mask_particles_seen_at_last_epoch[ind] = 1
            self.mask_tilts_seen_at_last_epoch[ind_tilt] = 1
            self.predicted_rots[ind_tilt] = rot_pred.reshape(-1, 3, 3)

            if self.n_classes > 1:
                self.predicted_rots_all_classes[ind_tilt] = rot_all_classes.reshape(-1, self.n_classes, 3, 3)
                self.predicted_idx_best_class[ind] = idx_best_class.reshape(-1)
                self.predicted_p_classes[ind] = p_classes.reshape(-1, self.n_classes)


            if not self.configs.no_trans:
                self.predicted_trans[ind_tilt] = trans_pred.reshape(-1, 2)
                if self.n_classes > 1:
                    self.predicted_trans_all_classes[ind_tilt] = trans_all_classes.reshape(-1, self.n_classes, 2)

            if self.configs.z_dim > 0:
                self.predicted_conf[ind] = conf_pred

                if self.configs.variational_het:
                    self.predicted_logvar[ind] = logvar_pred

        else:
            self.run_times['to_cpu'].append(0.0)

        # scalar summary
        if self.total_particles_count % self.configs.log_interval < batch_size:
            self.make_light_summary(all_losses)

    def detach_latent_variables(self, latent_variables_dict):
        if self.n_classes == 1:
            idx_best_class = None
            p_classes = None

            rot_pred = latent_variables_dict['R'].detach().cpu().numpy()
            trans_pred = (latent_variables_dict['t'].detach().cpu().numpy()
                          if not self.configs.no_trans else None)

            rot_all_classes = None
            trans_all_classes = None

            conf_pred = (latent_variables_dict['z'].detach().cpu().numpy()
                         if self.configs.z_dim > 0 and 'z' in latent_variables_dict
                         else None)

            logvar_pred = (latent_variables_dict['z_logvar'].detach().cpu().numpy()
                           if self.configs.z_dim > 0
                              and 'z_logvar' in latent_variables_dict
                           else None)
        else:
            idx_best_class = latent_variables_dict['idx_best_class'].detach().cpu().numpy()
            p_classes = latent_variables_dict['p'].detach().cpu().numpy()
            batch_size = latent_variables_dict['R'].shape[0]

            rot_pred = latent_variables_dict['R'][np.arange(batch_size), idx_best_class].detach().cpu().numpy()
            trans_pred = (latent_variables_dict['t'][np.arange(batch_size), idx_best_class].detach().cpu().numpy()
                          if not self.configs.no_trans else None)

            rot_all_classes = latent_variables_dict['R'].detach().cpu().numpy()
            trans_all_classes = (latent_variables_dict['t'].detach().cpu().numpy()
                          if not self.configs.no_trans else None)

            conf_pred = (latent_variables_dict['z'][np.arange(batch_size), idx_best_class].detach().cpu().numpy()
                         if self.configs.z_dim > 0 and 'z' in latent_variables_dict
                         else None)

            logvar_pred = (latent_variables_dict['z_logvar'][np.arange(batch_size), idx_best_class].detach().cpu().numpy()
                           if self.configs.z_dim > 0
                              and 'z_logvar' in latent_variables_dict
                           else None)

        return (rot_pred, trans_pred, conf_pred, logvar_pred, rot_all_classes, trans_all_classes, idx_best_class,
                p_classes)

    def forward_pass(self, in_dict):
        if self.configs.verbose_time:
            torch.cuda.synchronize()

        start_time_ctf = time.time()
        ctf_local = self.get_ctfs_at(in_dict['tilt_index'])

        if self.configs.subtomogram_averaging:
            ctf_local = ctf_local.reshape(
                -1, self.configs.n_tilts, *ctf_local.shape[1:])

        if self.configs.verbose_time:
            torch.cuda.synchronize()
            self.run_times['ctf'].append(time.time() - start_time_ctf)

        # forward pass
        if 'hypervolume' in self.optimized_modules:
            self.model.hypervolume.train()
        else:
            self.model.hypervolume.eval()

        if hasattr(self.model, 'conf_cnn'):
            if hasattr(self.model, 'conf_regressor'):
                if 'conf_encoder' in self.optimized_modules:
                    self.model.conf_cnn.train()
                    self.model.conf_regressor.train()
                else:
                    self.model.conf_cnn.eval()
                    self.model.conf_regressor.eval()

        if hasattr(self.model, 'pose_table'):
            if 'pose_table' in self.optimized_modules:
                self.model.pose_table.train()
            else:
                self.model.pose_table.eval()

        if hasattr(self.model, 'conf_table'):
            if 'conf_table' in self.optimized_modules:
                self.model.conf_table.train()
            else:
                self.model.conf_table.eval()

        if hasattr(self.model, 'score_table'):
            if 'score_table' in self.optimized_modules:
                self.model.score_table.train()
            else:
                self.model.score_table.eval()

        in_dict["ctf"] = ctf_local
        if self.n_prcs > 1:
            self.model.module.pose_only = self.pose_only
            self.model.module.use_point_estimates = self.use_point_estimates
            self.model.module.pretrain = self.pretraining
            self.model.module.is_in_pose_search_step = self.is_in_pose_search_step
            self.model.module.use_point_estimates_conf = (
                not self.configs.use_conf_encoder)

        else:
            self.model.pose_only = self.pose_only
            self.model.use_point_estimates = self.use_point_estimates
            self.model.pretrain = self.pretraining
            self.model.is_in_pose_search_step = self.is_in_pose_search_step
            self.model.use_point_estimates_conf = (
                not self.configs.use_conf_encoder)

        if self.configs.subtomogram_averaging:
            in_dict['tilt_index'] = in_dict['tilt_index'].reshape(
                *in_dict['y'].shape[0:2])

        out_dict = self.model(in_dict)
        self.run_times['encoder'].append(
            torch.mean(out_dict['time_encoder'].cpu())
            if self.configs.verbose_time else 0.
            )

        self.run_times['decoder'].append(
            torch.mean(out_dict['time_decoder'].cpu())
            if self.configs.verbose_time else 0.
            )

        self.run_times['decoder_coords'].append(
            torch.mean(out_dict['time_decoder_coords'].cpu())
            if self.configs.verbose_time else 0.
            )

        self.run_times['decoder_query'].append(
            torch.mean(out_dict['time_decoder_query'].cpu())
            if self.configs.verbose_time else 0.
            )

        latent_variables_dict = out_dict
        y_pred = out_dict['y_pred']
        y_gt_processed = out_dict['y_gt_processed']

        if (self.configs.subtomogram_averaging
                and self.configs.dose_exposure_correction):
            mask = self.output_mask.binary_mask
            a_pix = self.ctf_params[0, 0]

            dose_filters = self.data.get_dose_filters(
                in_dict['tilt_index'].reshape(-1),
                self.lattice,
                a_pix
                ).reshape(*y_pred.shape[:2], -1)

            if self.n_classes > 1:
                dose_filters = dose_filters[..., None, :]

            y_pred *= dose_filters[..., mask]

        return latent_variables_dict, y_pred, y_gt_processed

    def loss(self, y_pred, y_gt, latent_variables_dict):
        """
        y_pred: [batch_size, (n_tilts,) (n_classes,) n_pts]
        y_gt: [batch_size, (n_tilts,) (n_classes,) n_pts]
        R: [batch_size, (n_tilts,) (n_classes,) 3, 3]
        t: [batch_size, (n_tilts,) (n_classes,) 2]
        z: [batch_size, (n_classes) z_dim]
        z_logvar: [batch_size, (n_classes) z_dim]
        p: [batch_size, n_classes] (opt.)
        idx_best_class: [batch_size] (opt.)
        """
        all_losses = {}

        # data loss
        if self.n_classes == 1:
            data_loss = F.mse_loss(y_pred, y_gt)
        else:
            # std_noise = torch.std(y_gt)
            p = latent_variables_dict['p']
            if y_pred.dim() == 4:
                l2_dist = ((y_pred - y_gt) ** 2).mean(dim=(1, 3))
            else:
                l2_dist = ((y_pred - y_gt) ** 2).mean(dim=2)
            data_loss = -torch.logsumexp(torch.log(p) - l2_dist / (2. * self.std_noise ** 2), dim=-1).mean()
        all_losses['Data Loss'] = data_loss.item()
        total_loss = data_loss

        # KL divergence
        if self.use_kl_divergence:
            kld_conf = kl_divergence_conf(latent_variables_dict)
            total_loss += self.configs.beta_conf * kld_conf / self.resolution ** 2
            all_losses['KL Div. Conf.'] = kld_conf.item()

        # L1 regularization for translations
        if self.use_trans_l1_regularizer and self.use_point_estimates:
            trans_l1_loss = l1_regularizer(latent_variables_dict['t'])
            total_loss += self.configs.trans_l1_regularizer * trans_l1_loss
            all_losses['L1 Reg. Trans.'] = trans_l1_loss.item()

        # L2 smoothness prior
        if self.use_l2_smoothness_regularizer:
            smoothness_loss = l2_frequency_bias(y_pred, self.lattice.freqs2d,
                                                self.output_mask.binary_mask,
                                                self.resolution)
            total_loss += self.configs.l2_smoothness_regularizer * smoothness_loss
            all_losses['L2 Smoothness Loss'] = smoothness_loss.item()

        return total_loss, all_losses

    def make_heavy_summary(self):
        summary.make_img_summary(self.writer, self.in_dict_last,
                                 self.y_pred_last, self.output_mask,
                                 self.epoch)

        # conformation
        pca = None
        labels = None

        if self.configs.labels is not None:
            labels = utils.load_pkl(self.configs.labels)

            if self.index is not None:
                labels = labels[self.index]

        if self.mask_particles_seen_at_last_epoch is not None:
            mask_idx = self.mask_particles_seen_at_last_epoch > 0.5
        else:
            mask_idx = np.ones((self.n_particles_dataset, ), dtype=bool)
        labels = labels[mask_idx] if labels is not None else None
        idx_best_class = self.predicted_idx_best_class[mask_idx] if self.n_classes > 1 else None
        p_classes = self.predicted_p_classes[mask_idx] if self.n_classes > 1 else None
        logvar = (self.predicted_logvar[mask_idx]
                  if self.predicted_logvar is not None else None)
        summary.make_class_summary(self.writer, p_classes, self.epoch, labels, pca=None, logvar=logvar,
                                   palette_type=self.configs.color_palette, idx_best_class=idx_best_class, n_classes=self.n_classes)
        if self.configs.z_dim > 0:
            predicted_conf = self.predicted_conf[mask_idx]
            pca = summary.make_conf_summary(
                self.writer, predicted_conf, self.epoch, labels, pca=None, logvar=logvar,
                palette_type=self.configs.color_palette, idx_best_class=idx_best_class, n_classes=self.n_classes,
                p_classes=p_classes
                )

        # pose
        rotmat_gt = None
        trans_gt = None
        shift = (not self.configs.no_trans)

        if self.mask_particles_seen_at_last_epoch is not None:
            mask_tilt_idx = self.mask_tilts_seen_at_last_epoch > 0.5
        else:
            mask_tilt_idx = np.ones((self.n_tilts_dataset, ), dtype=bool)

        if self.configs.pose is not None:
            poses_gt = utils.load_pkl(self.configs.pose)

            if poses_gt[0].ndim == 3:
                # contains translations
                rotmat_gt = torch.tensor(poses_gt[0]).float()
                trans_gt = torch.tensor(poses_gt[1]).float() * self.resolution

                if self.index is not None:
                    rotmat_gt = rotmat_gt[self.index]
                    trans_gt = trans_gt[self.index]

            else:
                rotmat_gt = torch.tensor(poses_gt).float()
                trans_gt = None
                assert not shift, "Shift activated but trans not given in gt"

                if self.index is not None:
                    rotmat_gt = rotmat_gt[self.index]

            rotmat_gt = rotmat_gt[mask_tilt_idx]
            trans_gt = (trans_gt[mask_tilt_idx] if trans_gt is not None
                        else None)

        predicted_rots = self.predicted_rots[mask_tilt_idx]
        predicted_trans = (self.predicted_trans[mask_tilt_idx]
                           if self.predicted_trans is not None else None)

        summary.make_pose_summary(self.writer, predicted_rots, predicted_trans,
                                  rotmat_gt, trans_gt, self.epoch, shift=shift)

        return pca

    def make_light_summary(self, all_losses):
        self.logger.info(
            f"# [Train Epoch: {self.epoch}/{self.num_epochs - 1}] "
            f"[{self.current_epoch_particles_count}"
            f"/{self.n_particles_dataset} particles]"
            )

        if hasattr(self.output_mask, 'current_radius'):
            all_losses['Mask Radius'] = self.output_mask.current_radius

        if self.model.trans_search_factor is not None:
            all_losses['Trans. Search Factor'] = self.model.trans_search_factor

        summary.make_scalar_summary(self.writer, all_losses,
                                    self.total_particles_count)

        if self.configs.verbose_time:
            for key in self.run_times.keys():
                self.logger.info(
                    f"{key} time: {np.mean(np.array(self.run_times[key]))}")

    def save_latents(self):
        """Write model's latent variables to file."""
        out_pose = os.path.join(self.configs.outdir, f"pose.{self.epoch}.pkl")

        if self.configs.no_trans:
            with open(out_pose, 'wb') as f:
                pickle.dump(self.predicted_rots, f)
        else:
            with open(out_pose, 'wb') as f:
                pickle.dump((self.predicted_rots, self.predicted_trans), f)

        if self.configs.z_dim > 0:
            out_conf = os.path.join(self.configs.outdir,
                                    f"conf.{self.epoch}.pkl")
            with open(out_conf, 'wb') as f:
                pickle.dump(self.predicted_conf, f)

        if self.n_classes > 1:
            out_p_classes = os.path.join(self.configs.outdir,
                                         f"p_classes.{self.epoch}.pkl")
            with open(out_p_classes, 'wb') as f:
                pickle.dump(self.predicted_p_classes, f)

    def save_volume(self):
        """Write reconstructed volume to file."""
        self.model.hypervolume.eval()

        if self.configs.z_dim > 0:
            if self.n_classes == 1:
                zvals = [self.predicted_conf[0].reshape(-1)]
                kvals = [0]
            else:
                zvals = []
                kvals = []
                for k in range(self.n_classes):
                    if (self.predicted_idx_best_class == k).sum() > 0:
                        zvals.append(self.predicted_conf[self.predicted_idx_best_class == k][0].reshape(-1))
                        kvals.append(k)
        else:
            zvals = [None]
            kvals = [0]

        vols = self.model.eval_volume(self.data.norm, zvals=zvals, kvals=kvals)
        for k, vol in zip(kvals, vols):
            out_mrc = os.path.join(self.configs.outdir,
                                   f"reconstruct.epoch_{self.epoch}.class_{k}.mrc")
            mrc.write(out_mrc, vol.astype(np.float32))

    # TODO: weights -> model and reconstruct -> volume for output labels?
    def save_model(self):
        """Write model state to file."""
        out_weights = os.path.join(self.configs.outdir,
                                   f"weights.{self.epoch}.pkl")

        optimizers_state_dict = {}
        for key in self.optimizers.keys():
            optimizers_state_dict[key] = self.optimizers[key].state_dict()

        saved_objects = {
            'epoch': self.epoch,

            'model_state_dict': (self.model.module.state_dict()
                                 if self.n_prcs > 1
                                 else self.model.state_dict()),

            'hypervolume_state_dict': (
                self.model.module.hypervolume.state_dict() if self.n_prcs > 1
                else self.model.hypervolume.state_dict()
                ),

            'hypervolume_params': self.model.hypervolume[0].get_building_params(),
            'optimizers_state_dict': optimizers_state_dict,
            }

        if hasattr(self.output_mask, 'current_radius'):
            saved_objects[
                'output_mask_radius'] = self.output_mask.current_radius

        torch.save(saved_objects, out_weights)
