import datetime
import argparse

import os
import torch.multiprocessing as mp


import matplotlib

matplotlib.use("Agg")
import matplotlib.animation as animation
import matplotlib.pyplot as plt

import numpy as np
import torch
import torch.nn as nn

from tensordict import TensorDict
from torchrl.data import TensorDictReplayBuffer
from torchrl.data.replay_buffers.samplers import SamplerWithoutReplacement
from torchrl.data import SliceSamplerWithoutReplacement, SliceSampler, RandomSampler

from accelerate import Accelerator, DistributedDataParallelKwargs
from accelerate.state import AcceleratorState
from accelerate.utils import set_seed

from timeit import default_timer
from easydict import EasyDict

import wandb
from rich.progress import track
from easydict import EasyDict

from generative_operator.model.flow_model import (
    FunctionalFlow,
)
from generative_operator.model.point_cloud_flow_model import PointCloudFunctionalFlow
from generative_operator.utils.optimizer import CosineAnnealingWarmupLR
from generative_operator.dataset.tensordict_dataset import TensorDictDataset

from generative_operator.neural_networks.neural_operators.point_cloud_neural_operator import preprocess_data, compute_node_weights, compute_Fourier_modes
from generative_operator.neural_networks.neural_operators.point_cloud_data_process import compute_triangle_area_, compute_tetrahedron_volume_, compute_measure_per_elem_, compute_node_measures, convert_structured_data

from generative_operator.gaussian_process.matern import matern_halfinteger_kernel_batchwise
from generative_operator.utils.normalizer import UnitGaussianNormalizer


def data_preprocess(data_path):
    coordx    = np.load(data_path+"NACA_Cylinder_X.npy")
    coordy    = np.load(data_path+"NACA_Cylinder_Y.npy")
    data_out  = np.load(data_path+"NACA_Cylinder_Q.npy")[:,4,:,:] #density, velocity 2d, pressure, mach number

    nodes_list, elems_list, features_list = convert_structured_data([coordx, coordy], data_out[...,np.newaxis], nnodes_per_elem = 4, feature_include_coords = False)
    
    nnodes, node_mask, nodes, node_measures_raw, features, directed_edges, edge_gradient_weights = preprocess_data(nodes_list, elems_list, features_list)
    node_measures, node_weights = compute_node_weights(nnodes,  node_measures_raw,  equal_measure = False)
    node_equal_measures, node_equal_weights = compute_node_weights(nnodes,  node_measures_raw,  equal_measure = True)
    np.savez_compressed(data_path+"pcno_quad_data.npz", \
                        nnodes=nnodes, node_mask=node_mask, nodes=nodes, \
                        node_measures_raw = node_measures_raw, \
                        node_measures=node_measures, node_weights=node_weights, \
                        node_equal_measures=node_equal_measures, node_equal_weights=node_equal_weights, \
                        features=features, \
                        directed_edges=directed_edges, edge_gradient_weights=edge_gradient_weights) 


def load_data(data_path):
    equal_weights = True

    data = np.load(data_path + "pcno_triangle_data.npz")
    nnodes, node_mask, nodes = data["nnodes"], data["node_mask"], data["nodes"]

    node_weights = data["node_equal_weights"] if equal_weights else data["node_weights"]
    node_measures = data["node_measures"]
    node_measures_raw = data["node_measures_raw"]
    indices = np.isfinite(node_measures_raw)
    node_rhos = np.copy(node_weights)
    node_rhos[indices] = node_rhos[indices]/node_measures[indices]

    directed_edges, edge_gradient_weights = data["directed_edges"], data["edge_gradient_weights"]
    features = data["features"]
    return nnodes, node_mask, nodes, node_weights, node_rhos, features, directed_edges, edge_gradient_weights


def data_preparition(nnodes, node_mask, nodes, node_weights, node_rhos, features, directed_edges, edge_gradient_weights):
    print("Casting to tensor")
    nnodes = torch.from_numpy(nnodes)
    node_mask = torch.from_numpy(node_mask)
    nodes = torch.from_numpy(nodes.astype(np.float32))
    node_weights = torch.from_numpy(node_weights.astype(np.float32))
    node_rhos = torch.from_numpy(node_rhos.astype(np.float32))
    features = torch.from_numpy(features.astype(np.float32))
    directed_edges = torch.from_numpy(directed_edges.astype(np.int64))
    edge_gradient_weights = torch.from_numpy(edge_gradient_weights.astype(np.float32))

    # This is important
    nodes_input = nodes.clone()

    nodes_input = torch.cat([nodes_input, node_rhos], dim=-1)

    ndata = nodes_input.shape[0]
    ndata1 = 1931
    ndata2 = 1932

    n_train, n_test = 100, 40
    m_train, m_test = n_train // 2, n_test // 2


    # train_type == "flap"
    train_index = torch.arange(n_train)

    test_index = torch.cat(
        [torch.arange(ndata1 - m_test, ndata1), torch.arange(ndata1, ndata1 + m_test)], dim=0
    )

    x_train, x_test = nodes_input[train_index, ...], nodes_input[test_index, ...]
    aux_train = (
        node_mask[train_index, ...],
        nodes[train_index, ...],
        node_weights[train_index, ...],
        directed_edges[train_index, ...],
        edge_gradient_weights[train_index, ...],
    )
    aux_test = (
        node_mask[test_index, ...],
        nodes[test_index, ...],
        node_weights[test_index, ...],
        directed_edges[test_index, ...],
        edge_gradient_weights[test_index, ...],
    )

    # feature_type == "mach"
    feature_type_index = 1

    y_train, y_test = (
        features[train_index, ...][...,feature_type_index],
        features[test_index, ...][...,feature_type_index],
    )

    return x_train, x_test, aux_train, aux_test, y_train, y_test

def data_preparition_with_tensordict(nnodes, node_mask, nodes, node_weights, node_rhos, features, directed_edges, edge_gradient_weights):
    print("Casting to tensor")
    nnodes = torch.from_numpy(nnodes)
    node_mask = torch.from_numpy(node_mask)
    nodes = torch.from_numpy(nodes.astype(np.float32))
    node_weights = torch.from_numpy(node_weights.astype(np.float32))
    node_rhos = torch.from_numpy(node_rhos.astype(np.float32))
    features = torch.from_numpy(features.astype(np.float32))
    directed_edges = torch.from_numpy(directed_edges.astype(np.int64))
    edge_gradient_weights = torch.from_numpy(edge_gradient_weights.astype(np.float32))

    # This is important
    nodes_input = nodes.clone()

    nodes_input = torch.cat([nodes_input, node_rhos], dim=-1)

    ndata = nodes_input.shape[0]
    ndata1 = 1931
    ndata2 = 1932

    n_train, n_test = 100, 40
    m_train, m_test = n_train // 2, n_test // 2

    train_type = "flap"
    train_index = torch.arange(n_train)

    test_index = torch.cat(
        [torch.arange(ndata1 - m_test, ndata1), torch.arange(ndata1, ndata1 + m_test)], dim=0
    )

    feature_type = "mach"
    feature_type_index = 1

    train_data = TensorDict(
        {   "y": features[train_index, ...][...,feature_type_index],
            "condition": TensorDict(
                {
                    "x": nodes_input[train_index, ...],
                    "node_mask": node_mask[train_index, ...],
                    "nodes": nodes[train_index, ...],
                    "node_weights": node_weights[train_index, ...],
                    "directed_edges": directed_edges[train_index, ...],
                    "edge_gradient_weights": edge_gradient_weights[train_index, ...]
                },
                batch_size=(n_train,),
            ),
        },
        batch_size=(n_train,),
    )
    test_data = TensorDict(
        {   "y": features[test_index, ...][...,feature_type_index],
            "condition": TensorDict(
                {
                    "x": nodes_input[test_index, ...],
                    "node_mask": node_mask[test_index, ...],
                    "nodes": nodes[test_index, ...],
                    "node_weights": node_weights[test_index, ...],
                    "directed_edges": directed_edges[test_index, ...],
                    "edge_gradient_weights": edge_gradient_weights[test_index, ...]
                },
                batch_size=(n_test,),
            ),
        },
        batch_size=(n_test,),
    )

    n_train, n_test = train_data["condition"]["x"].shape[0], test_data["condition"]["x"].shape[0]

    if config.parameter.normalization_x:
        x_normalizer = UnitGaussianNormalizer(train_data["x"], non_normalized_dim = config.parameter.non_normalized_dim_x, normalization_dim=config.parameter.normalization_dim_x)
        x_train = x_normalizer.encode(train_data["condition"]["x"])
        x_test = x_normalizer.encode(test_data["condition"]["x"])
        x_normalizer.to(device)
    else:
        x_normalizer = None
        
    if config.parameter.normalization_y:
        y_normalizer = UnitGaussianNormalizer(train_data["y"], non_normalized_dim = config.parameter.non_normalized_dim_y, normalization_dim=config.parameter.normalization_dim_y)
        y_train = y_normalizer.encode(train_data["y"])
        y_test = y_normalizer.encode(test_data["y"])
        y_normalizer.to(device)
    else:
        y_normalizer = None

    train_dataset = TensorDictDataset(keys=["y", "condition"], max_size=n_train)
    test_dataset = TensorDictDataset(keys=["y", "condition"], max_size=n_test)
    train_dataset.append(train_data, batch_size=n_train)
    test_dataset.append(test_data, batch_size=n_test)

    return train_dataset, test_dataset, x_normalizer, y_normalizer



def model_initialization(device, x_train, y_train):

    kx_max, ky_max = 16, 16
    ndims = 2
    Lx, Ly = 1, 0.5
    print("Lx, Ly = ", Lx, Ly)
    modes = compute_Fourier_modes(ndims, [kx_max, ky_max], [Lx, Ly])
    modes = torch.tensor(modes, dtype=torch.float).to(device)

    flow_model_config = EasyDict(
        dict(
            device=device,
            gaussian_process=dict(
                type="Matern",
                args=dict(
                    length_scale=0.01,
                    nu=1.5,
                ),
            ),
            solver=dict(
                type="ODESolver",
                args=dict(
                    library="torchdiffeq",
                ),
            ),
            path=dict(
                sigma=1e-4,
                device=device,
            ),
            model=dict(
                type="velocity_function",
                args=dict(
                    backbone=dict(
                        type="PointCloudNeuralOperator",
                        args=dict(
                            ndims=ndims, 
                            modes=modes, 
                            nmeasures=1,
                            layers=[128,128,128,128,128],
                            fc_dim=128,
                            in_dim=y_train.shape[-1]+1+x_train.shape[-1], 
                            out_dim=y_train.shape[-1],
                            train_sp_L="together",
                            act='gelu'
                        ),
                    ),
                ),
            ),
        ),
    )

    model = PointCloudFunctionalFlow(
        config=flow_model_config,
    )

    return model, flow_model_config

if __name__ == "__main__":

    ddp_kwargs = DistributedDataParallelKwargs(find_unused_parameters=True)
    accelerator = Accelerator(log_with=None, kwargs_handlers=[ddp_kwargs])
    device = accelerator.device
    state = AcceleratorState()

    # Get the process rank
    process_rank = state.process_index
    set_seed(seed=42+process_rank)
    print(f"Process rank: {process_rank}")

    project_name = "PCNO_airfoil_flap"

    # check GPU brand, if NVIDIA RTX 4090 use batch size 4, if NVIDIA A100 use batch size 16
    if torch.cuda.is_available():
        gpu_name = torch.cuda.get_device_name(0)
        if "A100" in gpu_name:
            batch_size = 16
        elif "4090" in gpu_name:
            batch_size = 1
        else:
            batch_size = 1
    else:
        batch_size = 1
    print(f"GPU name: {gpu_name}, batch size: {batch_size}")


    config = EasyDict(
        dict(
            device=device,
            parameter=dict(
                batch_size=batch_size,
                warmup_steps=10000,
                learning_rate=5e-5 * accelerator.num_processes,
                iterations=100001,
                log_rate=100,
                eval_rate=100000,
                checkpoint_rate=50000,
                video_save_path=f"output/{project_name}/videos",
                model_save_path=f"output/{project_name}/models",
                model_load_path=None,
                normalization_x=False,
                normalization_y=False,
                normalization_dim_x=[],
                normalization_dim_y=[],
                non_normalized_dim_x=1,
                non_normalized_dim_y=0,
            ),
        )
    )

    data_path = "../../../NeuralOperator/data/airfoil_flap/"
    # data_preprocess(data_path)
    nnodes, node_mask, nodes, node_weights, node_rhos, features, directed_edges, edge_gradient_weights = load_data(data_path)

    train_dataset, test_dataset, x_normalizer, y_normalizer = data_preparition_with_tensordict(nnodes, node_mask, nodes, node_weights, node_rhos, features, directed_edges, edge_gradient_weights)


    flow_model, flow_model_config = model_initialization(device, train_dataset["condition"]["x"], train_dataset["y"])

    if config.parameter.model_load_path is not None and os.path.exists(
        config.parameter.model_load_path
    ):
        # pop out _metadata key
        state_dict = torch.load(config.parameter.model_load_path, map_location="cpu")
        state_dict.pop("_metadata", None)
        flow_model.model.load_state_dict(state_dict)

    optimizer = torch.optim.Adam(
        flow_model.model.parameters(), lr=config.parameter.learning_rate
    )

    scheduler = CosineAnnealingWarmupLR(
        optimizer,
        T_max=config.parameter.iterations,
        eta_min=2e-6,
        warmup_steps=config.parameter.warmup_steps,
    )

    flow_model.model, optimizer = accelerator.prepare(flow_model.model, optimizer)

    os.makedirs(config.parameter.model_save_path, exist_ok=True)

    train_replay_buffer = TensorDictReplayBuffer(
        storage=train_dataset.storage,
        batch_size=config.parameter.batch_size,
        sampler=RandomSampler(),
        prefetch=10,
    )

    test_replay_buffer = TensorDictReplayBuffer(
        storage=test_dataset.storage,
        batch_size=config.parameter.batch_size,
        sampler=RandomSampler(),
        prefetch=10,
    )


    accelerator.init_trackers("PCNO_airfoil_flap_flow", config=None)
    accelerator.print("✨ Start training ...")

    for iteration in track(
        range(config.parameter.iterations),
        description="Training",
        disable=not accelerator.is_local_main_process,
    ):
        flow_model.train()
        with accelerator.autocast():
            with accelerator.accumulate(flow_model.model):
                
                data = train_replay_buffer.sample()
                data = data.to(device)

                matern_kernel = matern_halfinteger_kernel_batchwise(
                    X1=data["condition"]["nodes"],
                    X2=data["condition"]["nodes"],
                    length_scale=flow_model_config.gaussian_process.args.length_scale,
                    nu=flow_model_config.gaussian_process.args.nu,
                    variance=1.0,
                )

                def sample_from_covariance(C, D):
                    # Compute Cholesky decomposition; shape [B, N, N]
                    L = torch.linalg.cholesky(C+1e-6*torch.eye(C.size(1), device=C.device, dtype=C.dtype).unsqueeze(0))
                    
                    # Generate standard normal noise; shape [B, N, D]
                    z = torch.randn(C.size(0), C.size(1), D*2, device=C.device, dtype=C.dtype)
                    
                    # Batched matrix multiplication; result shape [B, N, 2D]
                    samples = L @ z

                    # split the samples into two parts
                    samples = torch.split(samples, [D, D], dim=-1)
                    
                    return samples[0], samples[1]

                # gaussian_process = flow_model.gaussian_process(data["nodes"])
                x0, gaussian_process_samples = sample_from_covariance(matern_kernel, data["y"].shape[-1])

                if y_normalizer is not None:
                    x1 = y_normalizer.encode(data["y"])
                else:
                    x1 = data["y"]

                loss = flow_model.functional_flow_matching_loss(x0=x0, x1=x1, condition=data["condition"], gaussian_process_samples=gaussian_process_samples, mse_loss=True)
                optimizer.zero_grad()
                accelerator.backward(loss)
                optimizer.step()
                scheduler.step()


        loss = accelerator.gather(loss)
        if iteration % config.parameter.log_rate == 0:
            if accelerator.is_local_main_process:
                to_log = {
                        "loss/mean": loss.mean().item(),
                        "iteration": iteration,
                        "lr": scheduler.get_last_lr()[0],
                    }
                
                if len(loss.shape) == 0:
                    to_log["loss/std"] = 0
                    to_log["loss/0"] = loss.item()
                elif loss.shape[0] > 1:
                    to_log["loss/std"] = loss.std().item()
                    for i in range(loss.shape[0]):
                        to_log[f"loss/{i}"] = loss[i].item()
                accelerator.log(
                    to_log,
                    step=iteration,
                )
                acc_train_loss = loss.mean().item()
                print(f"iteration: {iteration}, train_loss: {acc_train_loss:.5f}, lr: {scheduler.get_last_lr()[0]:.7f}")

        if iteration % config.parameter.eval_rate == 0:
            pass
            # sampled_process = flow_model.sample_process(
            #     x0=x0,
            #     t_span=torch.linspace(0.0, 1.0, 10),
            #     condition=data["condition"],
            #     with_grad=True,
            # )

        if iteration % config.parameter.checkpoint_rate == 0:
            if accelerator.is_local_main_process:
                if not os.path.exists(config.parameter.model_save_path):
                    os.makedirs(config.parameter.model_save_path)
                torch.save(
                    accelerator.unwrap_model(flow_model.model).state_dict(),
                    f"{config.parameter.model_save_path}/model_{iteration}.pth",
                )

        accelerator.wait_for_everyone()

    accelerator.print("✨ Training complete!")
    accelerator.end_training()