# Hydra: Mixture of Neural Fields for Heterogeneous Reconstruction in Cryo-EM #

Hydra is a neural network-based algorithm for _ab initio_ heterogeneous single-particle cryo-EM reconstruction. Building on top of [DRGN-AI](https://github.com/ml-struct-bio/drgnai), the
method parameterizes structures as arising from one of _K_ neural fields, thus jointly modeling both compositional and conformational heterogeneity and enabling the reconstruction of mixtures of flexible biomolecules.

## Documentation ##

Hydra borrows its user interface and workflow from DRGN-AI. The latest detailed documentation for DRGN-AI is available [on gitbook](https://ez-lab.gitbook.io/drgn-ai/), 
including an overview and walkthrough of installation, training and analysis. A brief quick start is
provided below.

## Installation ##

We recommend installing Hydra in a clean conda environment — first clone the git repository, and then use `pip` to install the package from the source code:

    (base) $ conda create --name hydra python=3.9
    (base) $ conda activate hydra
    (hydra) $ git clone git@github.com:ml-struct-bio/hydra.git --branch main --single-branch
    (hydra) $ cd hydra
    (hydra) $ pip install . 

To confirm that the package was installed successfully, use `drgnai test`:

```
(drgnai) $ drgnai test
Installation was successful!
```

You may also choose to define an environment variable `$DRGNAI_DATASETS` in your bash environment, which will allow you
to point to a file listing locations of input files and dataset labels to use as shortcuts. For more information, 
see our [detailed user guide](https://ez-lab.gitbook.io/drgn-ai/).


## Usage ##

This package installs the `drgnai` command line tool for running experiments, which contains three key subcommands:

 - `drgnai setup` creates the experiment folder and configuration parameter file
 - `drgnai train` trains and analyzes a reconstruction model
 - `drgnai analyze` performs specific analyses in addition to those done by `train`

As cryo-EM reconstruction experiments are usually computationally intensive, `train` especially is most
commonly used within a script submitted to a job scheduling system on a high-performance compute cluster.


### Setup ###

Before running an experiment, you must first create a directory for your experiment's output, as well as a configuration
file governing how the experiment will be run. You can complete these two steps manually; for users unfamiliar with
using terminals or with editing files using tools like `vim` or `nano` we recommend using the `drgnai setup` tool 
instead. For example,

```
drgnai setup out-dir --particles /my_data/particles.mrcs --ctf /my_data/ctf.pkl \
                     --conf-estimation autodecoder --pose-estimation abinit \
                     --reconstruction-type het                               
```

This command will create an output directory called `out-dir` and a configuration file `out-dir/configs.yaml`:

```yaml
particles: /my_data/particles.mrcs
ctf: /my_data/ctf.pkl
quick_config:
  capture_setup: spa
  conf_estimation: autodecoder
  pose_estimation: abinit
  reconstruction_type: het
```


### Reconstruction and analysis ###

After setup is complete, run the experiment using `drgnai train out-dir`. You can perform analysis on a particular training epoch and class using the `analyze` command:

```
drgnai train out-dir
drgnai analyze out-dir --epoch 25 --class-idx 3
```

`drgnai` will save the outputs of training under `out-dir/out`; outputs of each analysis will be stored under 
`out-dir/out/analysis_<epoch>/`.


### Monitoring running experiments ###

The progress of model training can be tracked using the `out-dir/out/training.log` file.

The training step can also be monitored while it is running using Tensorboard, which is installed as part of DRGN-AI,
by following these steps:

1. Run the command `tensorboard --logdir out-dir/out --port 6565 --bind_all` remotely, where out-dir is the experiment 
output directory and 6565 is an arbitrary port number.
2. Run the command `ssh -NfL 6565:<server-name>:6565 <user-name>@<server-address>` locally, using the same port number 
   above, and replacing the server info with your own.
3. Navigate to localhost:6565 in your local browser to access the tensorboard interface.


## Configuration ##

The behaviour of the algorithm can be modified by passing different values to `drgnai setup` at the beginning of the
experiment. However, only the most important parameters are available through this interface:

 - `--reconstruction-type` “het” for heterogeneous or “homo” for homogeneous (default)
 - `--pose-estimation` “abinit” for no initialization (default), “refine” to refine provided poses by gradient
                       descent or “fixed” to use provided poses without refinement
 - `--conf-estimation` “autodecoder” (default), “encoder” or “refine” to refine conformations by
                       gradient descent (you must then define initial_conf) — not used in homogeneous reconstruction

Note that each argument can be specified using a non-ambiguous prefix, e.g.
```
drgnai setup out-dir --dataset 50S_128 --conf autodecoder \
                     --pose-estim abinit --reconstr het
```

To change the other configuration parameters, the `configs.yaml` file must be edited directly before the experiment
is run. The base parameters of DRGN-AI are described in the [docs](https://ez-lab.gitbook.io/drgn-ai/configuration).
Additional parameters used in Hydra are described below:

 - `n_classes`: [int, default = 1] Number of classes. Should be at least as many distinct particle types you expect in the data.
 - `lr_score_table`: [float, default = 1.0e-2] Learning rate of the class score table.
 - `std_noise`: [float, default = 1.0] Higher values encourage more uniform class posteriors, and vice versa.


## Reference ##

Mixture of Neural Fields for Heterogeneous Reconstruction in Cryo-EM. Axel Levy*, Rishwanth Raghu*, David Shustin*, Adele Rui-Yang Peng, Huan Li, Oliver Clarke, Gordon Wetzstein, Ellen D. Zhong. NeurIPS, 2024.

## Contact ##

For any feedback, questions, or bugs, please file a Github issue.
