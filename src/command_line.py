"""DRGN-AI neural network reconstruction experiment pipeline"""

import argparse
import os
import yaml

from cryodrgn import utils
from .reconstruct import ModelTrainer
from .analyze import ModelAnalyzer
from .configuration import AnalysisConfigurations, TrainingConfigurations
from .visualization import interactive_filtering
from .utils import checksum

CONFIG_DIR = os.path.join(os.path.abspath(os.path.dirname(__file__)),
                          'configs')


def run_cryodrgn_ai() -> None:
    """Interface for commands used to run drgnai experiments."""

    main_parser = argparse.ArgumentParser(description=__doc__)
    parent_parser = argparse.ArgumentParser(
        description=__doc__, add_help=False)

    parent_parser.add_argument('outdir', help="experiment output location")
    subparsers = main_parser.add_subparsers(
        help='choose a command', required=True)

    setup_parser = subparsers.add_parser(
        'setup', description="Setup the experiment.", parents=[parent_parser])
    setup_parser.set_defaults(func=setup_experiment)

    setup_parser.add_argument('--dataset',
                              help="which dataset to run the experiment on")

    setup_parser.add_argument(
        '--particles', help="path to the picked particles (.mrcs/.star /.txt)")
    setup_parser.add_argument('--ctf',
                              help="path to the CTF parameters (.pkl)")
    setup_parser.add_argument('--pose', help="path to the poses (.pkl)")

    setup_parser.add_argument(
        '--capture-setup', default='spa', choices=['spa', 'et'],
        help="`spa` for single-particle imaging (default) "
             "or `et` for electron tomography",
        dest='capture_setup'
        )

    setup_parser.add_argument(
        '--reconstruction-type', default='homo', choices=['het', 'homo'],
        help="homogeneous (default) or heterogeneous reconstruction?",
        dest='rcnstr_type'
        )

    setup_parser.add_argument(
        '--pose-estimation', default='abinit',
        choices=['abinit', 'refine', 'fixed'],
        help="`abinit` for no initialization (default), `refine` to refine "
             "ground truth poses by gradient descent or `fixed` to use ground "
             "truth poses",
        dest='pose_estim'
        )

    setup_parser.add_argument(
        '--conf-estimation', default='autodecoder',
        choices=['encoder', 'autodecoder', 'refine'],
        help="conformation estimation mode for heterogenous reconstruction: "
             "`autodecoder` (default), `encoder` or `refine` to refine "
             "conformations by gradient descent "
             "(you must then define initial_conf)",
        dest='conf_estim'
        )

    train_parser = subparsers.add_parser(
        'train', description="Train the experiment.", parents=[parent_parser])
    train_parser.set_defaults(func=train_experiment)

    train_parser.add_argument(
        '--no-analysis', action='store_true',
        help="just do the training stage",
        )

    analyze_parser = subparsers.add_parser(
        'analyze', description="Analyze the experiment.",
        parents=[parent_parser]
        )
    analyze_parser.set_defaults(func=analyze_experiment)

    analyze_parser.add_argument('--epoch', '-e', type=int, default=-1,
                                help="which training epoch to analyze; the"
                                     "default is to analyze the last"
                                     "completed epoch")

    analyze_parser.add_argument('--skip-umap',
                                action='store_true', dest='skip_umap')

    analyze_parser.add_argument('--pc', type=int, default=2,
                                help="how many components to use "
                                     "in the PCA analysis")
    analyze_parser.add_argument('--n-per-pc', type=int, default=10,
                                help="how many plots to make "
                                     "for each PCA component")

    analyze_parser.add_argument('--ksample', type=int, default=20)
    analyze_parser.add_argument('--seed', type=int, default=-1)
    analyze_parser.add_argument('--invert', action='store_true')

    analyze_parser.add_argument('--sample-z-idx', type=int, nargs='+', default=None,
                                dest='sample_z_idx')
    analyze_parser.add_argument('--trajectory-1d', type=int, nargs=3, default=None,
                                dest='trajectory_1d')

    analyze_parser.add_argument('--direct-traversal-txt',
                                type=str, default=None,
                                dest='direct_traversal_txt')
    analyze_parser.add_argument('--z-values-txt', type=str, default=None,
                                dest='z_values_txt')

    analyze_parser.add_argument('--class-idx', type=int, default=0,
                                dest='class_idx')

    filter_parser = subparsers.add_parser(
        'filter', description="Interactive filtering of mapped particles.",
        parents=[parent_parser]
        )
    filter_parser.set_defaults(func=filter_experiment)

    filter_parser.add_argument('--epoch', '-e', type=int, default=-1,
                               help="which train epoch to use for filtering")
    filter_parser.add_argument('--kmeans', '-k', type=int, default=-1,
                               help="which set of k-means clusters "
                                    "to use for filtering")

    filter_parser.add_argument("--plot-inds", type=str,
                               help="path to a file containing previously "
                                    "selected indices that will be plotted at "
                                    "the beginning",
                               dest='plot_inds')

    test_parser = subparsers.add_parser(
        'test', description="Test the package installation.")
    test_parser.set_defaults(func=test_package)

    csum_parser = subparsers.add_parser(
        'checksum', description="Get a hash of an experiment's output.",
        parents=[parent_parser]
        )
    csum_parser.set_defaults(func=checksum_experiment)

    args = main_parser.parse_args()
    args.func(args)


def setup_experiment(args, write_config=True) -> dict:
    """drgnai setup: create experiment resources"""

    os.makedirs(args.outdir, exist_ok=True)
    configs_file = os.path.join(args.outdir, 'configs.yaml')

    if os.path.exists(configs_file):
        with open(configs_file, 'r') as f:
            configs = yaml.safe_load(f)

        if 'dataset' not in configs and hasattr(args, 'dataset'):
            configs['dataset'] = args.dataset

    else:
        configs = {'dataset': args.dataset, 'particles': args.particles,
                   'ctf': args.ctf, 'pose': args.pose,
                   'quick_config': {'capture_setup': args.capture_setup,
                                    'reconstruction_type': args.rcnstr_type,
                                    'pose_estimation': args.pose_estim,
                                    'conf_estimation': args.conf_estim}}

    # turn anything that looks like a relative path into an absolute path
    for k in list(configs):
        if isinstance(configs[k], str):
            new_path = os.path.abspath(os.path.join(configs_file, configs[k]))

            if os.path.exists(new_path):
                configs[k] = new_path

    if configs['quick_config']['reconstruction_type'] == 'homo':
        configs['quick_config']['conf_estimation'] = None

    paths_file = os.environ.get("DRGNAI_DATASETS")
    if paths_file:
        with open(paths_file, 'r') as f:
            data_paths = yaml.safe_load(f)
    else:
        data_paths = None

    # handling different ways of specifying the input data, starting with a
    # file containing the data files
    if 'dataset' in configs and configs['dataset']:
        if os.path.exists(configs['dataset']):
            with open(configs['dataset'], 'r') as f:
                paths = yaml.safe_load(f)

            # resolve paths relative to the dataset file if they look relative
            for k in list(paths):
                if paths[k] and not os.path.isabs(paths[k]):
                    paths[k] = os.path.abspath(
                        os.path.join(configs['dataset'], paths[k]))

            del configs['dataset']

        elif data_paths and configs['dataset'] not in data_paths:
            raise ValueError(f"Given dataset {configs['dataset']} is not a "
                             "label in the list of known datasets!")

        elif data_paths is None:
            raise ValueError("To specify datasets using a label, first specify"
                             "a .yaml catalogue of datasets using the "
                             "environment variable $DRGNAI_DATASETS!")

        # you can also give the dataset as a label in the global dataset list
        else:
            paths = data_paths[configs['dataset']]

    # one can also specify the dataset files themselves in the config file
    elif 'particles' in configs and 'ctf' in configs:
        paths = {'particles': configs['particles'], 'ctf': configs['ctf']}

        if 'pose' in configs and configs['pose']:
            paths['pose'] = configs['pose']

        if 'dataset' in configs:
            del configs['dataset']

    elif (not hasattr(args, 'particles') or args.particles is None
            or not hasattr(args, 'ctf') or args.ctf is None):
        raise ValueError("Must specify either a dataset label stored in "
                         f"{paths_file} or the paths to a particles and "
                         "ctf settings file!")

    # finally, these files can also be specified from the command line
    else:
        paths = {'particles': args.particles, 'ctf': args.ctf}

        if args.pose:
            paths['pose'] = args.pose

        if 'dataset' in configs:
            del configs['dataset']

    for k in list(paths):
        if isinstance(paths[k], str):
            paths[k] = os.path.abspath(paths[k])

    # create the final configurations and test that they are valid before
    # saving them to file
    configs = {**configs, **paths}
    if "outdir" not in configs:
        configs['outdir'] = os.path.join(args.outdir, 'out')
    _ = TrainingConfigurations(configs)

    os.makedirs(os.path.join(args.outdir, 'out'), exist_ok=True)
    if write_config:
        with open(configs_file, 'w') as f:
            yaml.dump(configs, f, sort_keys=False)

    return configs


def train_experiment(args) -> None:
    """drgnai train: train model for estimating particle poses and volumes"""

    configs = setup_experiment(args, write_config=False)
    utils._verbose = False
    trainer = ModelTrainer(configs)
    trainer.train()

    if not args.no_analysis:
        analyze_experiment(args)


def analyze_experiment(args) -> None:
    """drgnai analyze: analyze, interpret, and visualize the trained model"""

    train_configs_file = os.path.join(args.outdir, 'out', 'train-configs.yaml')
    if not os.path.exists(train_configs_file):
        raise ValueError("Missing train-configs.yaml file "
                         "in given output folder!")

    with open(train_configs_file, 'r') as f:
        train_configs = yaml.safe_load(f)

    train_configs['outdir'] = os.path.join(args.outdir, 'out')
    anlz_configs = {par: (getattr(args, par) if hasattr(args, par)
                          else AnalysisConfigurations.defaults[par])
                    for par in AnalysisConfigurations.parameters}

    utils._verbose = False

    analyzer = ModelAnalyzer(anlz_configs, train_configs)
    analyzer.analyze()

    # HACK
    # if hasattr(args, 'class_idx'):
    #     print(f"Analyzing class_idx {anlz_configs['class_idx']}.")
    #     analyzer = ModelAnalyzer(anlz_configs, train_configs)
    #     analyzer.analyze()
    # else:
    #     n_classes = train_configs['n_classes']
    #     print(f"Analyzing class_idx [0, ..., {n_classes - 1}].")
    #     for class_idx in range(n_classes):
    #         anlz_configs['class_idx'] = class_idx
    #         analyzer = ModelAnalyzer(anlz_configs, train_configs)
    #         analyzer.analyze()


def filter_experiment(args) -> None:
    """drgnai filter: interactive filtering of particles using model results"""

    interactive_filtering(args.outdir, args.epoch, args.kmeans, args.plot_inds)


def test_package(args) -> None:
    """drgnai test: check if package was installed correctly"""

    utils._verbose = False
    trainer = ModelTrainer({'test_installation': True})
    trainer.train()


def checksum_experiment(args) -> None:
    """drgnai checksum: print a hash value of the output of an experiment"""

    if 'out' in os.listdir(args.outdir):
        print(checksum(os.path.join(args.outdir, 'out')))

    # compatibility with older versions of cryodrgnai
    else:
        print(checksum(os.path.join(args.outdir)))
