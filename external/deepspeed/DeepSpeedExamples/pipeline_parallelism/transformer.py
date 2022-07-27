""" Simple transformer structure for development purpose
References:
- http://nlp.seas.harvard.edu/2018/04/03/attention.html#model-architecture
"""

from datetime import timedelta
import os
from pathlib import Path
import copy
import time
import math
import argparse
import numpy as np
import json

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.distributed as dist

import deepspeed
from deepspeed.pipe import PipelineModule
from deepspeed.runtime.pipe.topology import ProcessTopology, PipeDataParallelTopology


def get_args():
    parser = argparse.ArgumentParser(description='FFN')
    parser.add_argument('-s',
                        '--steps',
                        type=int,
                        default=10,
                        help='quit after this many steps')
    parser.add_argument('--curr-step', '-cs',
                        type=int,
                        default=0,
                        help='The step to start on')
    parser.add_argument('--backend',
                        type=str,
                        default='nccl',
                        help='distributed backend')
    ## Elastic stuff
    parser.add_argument('-rl', '--redundancy_level',
                        type=int,
                        default=0)
    parser.add_argument('--eager',
                        action='store_true',
                        default=False)
    parser.add_argument('--debug',
                        action='store_true',
                        default=False)
    parser.add_argument('--mem_log',
                        action='store_true',
                        default=False)
    parser.add_argument('--seed', type=int, default=1138, help='PRNG seed')

    ## Model config args
    parser.add_argument('-N', type=int, default=8)
    parser.add_argument('--d-model', '-dm', type=int, default=1024)
    parser.add_argument('--d-ff', '-dff', type=int, default=2048)
    parser.add_argument('-H', type=int, default=16)
    parser.add_argument('-seq', type=int, default=512)
    parser.add_argument('--parts',
                        type=str,
                        default='',
                        help='Specify number of layers for each partition; separated by comma like `1,2,2,3`')

    parser = deepspeed.add_config_arguments(parser)
    args = parser.parse_args()
    return args


def clones(module, N):
    "Produce N identical layers."
    return nn.ModuleList([copy.deepcopy(module) for _ in range(N)])


class LayerNorm(nn.Module):
    "Construct a layernorm module (See citation for details)."
    def __init__(self, features, eps=1e-6):
        super(LayerNorm, self).__init__()
        self.a_2 = nn.Parameter(torch.ones(features))
        self.b_2 = nn.Parameter(torch.zeros(features))
        self.eps = eps

    def forward(self, x):
        mean = x.mean(-1, keepdim=True)
        std = x.std(-1, keepdim=True)
        return self.a_2 * (x - mean) / (std + self.eps) + self.b_2


class SublayerConnection(nn.Module):
    """
    A residual connection followed by a layer norm.
    Note for code simplicity the norm is first as opposed to last.
    """
    def __init__(self, size, dropout):
        super(SublayerConnection, self).__init__()
        self.norm = LayerNorm(size)
        self.dropout = nn.Dropout(dropout)

    def forward(self, x, sublayer):
        "Apply residual connection to any sublayer with the same size."
        return x + self.dropout(sublayer(self.norm(x)))


class MultiHeadedAttention(nn.Module):
    def __init__(self, h, d_model, dropout=0.1):
        "Take in model size and number of heads."
        super().__init__()
        assert d_model % h == 0
        self.d_k = d_model // h
        self.h = h
        self.linears = clones(nn.Linear(d_model, d_model), 4)
        self.attn = None
        self.dropout = nn.Dropout(p=dropout)

    def forward(self, query, key, value, mask=None):
        def attention(query, key, value, mask=None, dropout=None):
            "Compute 'Scaled Dot Product Attention'"
            d_k = query.size(-1)
            scores = torch.matmul(query, key.transpose(-2, -1)) / math.sqrt(d_k)
            if mask is not None:
                scores = scores.masked_fill(mask == 0, -1e9)
            p_attn = F.softmax(scores, dim=-1)
            if dropout is not None:
                p_attn = dropout(p_attn)
            return torch.matmul(p_attn, value), p_attn

        if mask is not None:
            # Same mask applied to all h heads.
            mask = mask.unsqueeze(1)
        nbatches = query.size(0)

        # 1) Do all the linear projections in batch from d_model => h x d_k
        query, key, value = \
            [l(x).view(nbatches, -1, self.h, self.d_k).transpose(1, 2)
             for l, x in zip(self.linears, (query, key, value))]

        # 2) Apply attention on all the projected vectors in batch.
        x, self.attn = attention(query, key, value, mask=mask,
                                 dropout=self.dropout)

        # 3) "Concat" using a view and apply a final linear.
        x = x.transpose(1, 2).contiguous() \
             .view(nbatches, -1, self.h * self.d_k)
        return self.linears[-1](x)


class PositionwiseFeedForward(nn.Module):
    "Implements FFN equation."
    def __init__(self, d_model, d_ff, dropout=0.1):
        super().__init__()
        self.w_1 = nn.Linear(d_model, d_ff)
        self.w_2 = nn.Linear(d_ff, d_model)
        self.dropout = nn.Dropout(dropout)

    def forward(self, x):
        return self.w_2(self.dropout(F.relu(self.w_1(x))))


class EncoderLayer(nn.Module):
    "Encoder is made up of self-attn and feed forward"
    def __init__(self, size, self_attn, feed_forward, dropout):
        super(EncoderLayer, self).__init__()
        self.self_attn = self_attn
        self.feed_forward = feed_forward
        self.sublayer = clones(SublayerConnection(size, dropout), 2)
        self.size = size

    def forward(self, x, mask=None):
        x = self.sublayer[0](x, lambda x: self.self_attn(x, x, x, mask))
        return self.sublayer[1](x, self.feed_forward)

    def sum_params(self):
        params_sum = torch.tensor([0.0]).cuda()
        for p in self.parameters():
            params_sum += p.sum()

        return params_sum.cpu().item()

class Encoder(nn.Module):
    "Core encoder is a stack of N layers"
    def __init__(self, layer, N):
        super(Encoder, self).__init__()
        self.layers = clones(layer, N)
        self.norm = LayerNorm(layer.size)

    def forward(self, x, mask=None):
        "Pass the input (and mask) through each layer in turn."
        for layer in self.layers:
            x = layer(x, mask)
        return self.norm(x)

    def join_layers(self):
        return [i for i in self.layers] + [self.norm]


def make_encoder(N, d_model, d_ff, h, dropout=0.1):
    c = copy.deepcopy
    attn = MultiHeadedAttention(h, d_model)
    ff = PositionwiseFeedForward(d_model, d_ff, dropout)
    return Encoder(EncoderLayer(d_model, c(attn), c(ff), dropout), N)

def test_encoder(args):
    class DummyDataset(torch.utils.data.Dataset):
        def __init__(self, seq, d_model, size=1000):
            self._size = size
            self._inputs = np.random.randn(size, seq, d_model)
            self._labels = np.random.randn(size, seq)
            # TODO(pengzhan): Figure out how to feed mask

        def __len__(self):
            return self._size

        def __getitem__(self, idx):
            return (
                torch.tensor(self._inputs[idx], dtype=torch.float32),
                self._labels[idx].astype('float32')
            )

    import json
    from torch.distributed.elastic.rendezvous import RendezvousParameters
    from project_pactum.rendezvous.etcd import create_rdzv_handler

    rdzv_backend = 'etcd-v2'
    rdzv_endpoint = os.environ['PROJECT_PACTUM_ENDPOINT']
    run_id = os.environ['PROJECT_PACTUM_RUN_ID']
    min_nodes = int(os.environ['PROJECT_PACTUM_MIN_NODES'])
    max_nodes = int(os.environ['PROJECT_PACTUM_MAX_NODES'])
    rdzv_configs = json.loads(os.environ['PROJECT_PACTUM_RDZV_CONFIGS'])
    rdzv_configs['last_call_timeout'] = 1
    rdzv_parameters = RendezvousParameters(
        backend=rdzv_backend,
        endpoint=rdzv_endpoint,
        run_id=run_id,
        min_nodes=min_nodes,
        max_nodes=max_nodes,
        **rdzv_configs,
    )
    rdzv_handler = create_rdzv_handler(rdzv_parameters)
    deepspeed.init_distributed(
        args.backend,
        rank=int(os.environ['RANK']),
        world_size=int(os.environ['WORLD_SIZE']),
        store=rdzv_handler.setup_kv_store(),
    )

    encoder = make_encoder(args.N, args.d_model, args.d_ff, args.H)

    layers = [i for i in encoder.layers] + [encoder.norm, lambda x: x.sum(-1)]

    data_parallel_size = int(os.environ['PROJECT_PACTUM_NUM_PIPELINES'])
    pipe_parallel_size = int(os.environ['PROJECT_PACTUM_NUM_STAGES'])
    global_decision = rdzv_handler.get_global_decision()
    custom_topology = PipeDataParallelTopology(pipe_parallel_size, data_parallel_size, global_decision)

    parts = []
    if args.parts:
        parts = [int(p) for p in args.parts.split(',')]
        assert sum(parts) == args.N
        parts[-1] += 2
        parts = [0] + [sum(parts[:i]) + p for i, p in enumerate(parts)]
    model = PipelineModule(layers=layers,
                           loss_fn=nn.MSELoss(),
                           num_stages=pipe_parallel_size,
                           partition_method='uniform' if not parts else 'custom',
                           custom_partitions=parts,
                           topology=custom_topology,
                           activation_checkpoint_interval=0)

    np.random.seed(args.seed)
    dataset = DummyDataset(args.seq, args.d_model)

    engine, _, _, _ = deepspeed.initialize(
        args=args,
        model=model,
        model_parameters=[p for p in model.parameters() if p.requires_grad],
        training_data=dataset,
        redundancy_level=args.redundancy_level,
        eager_recovery=args.eager)

    for _ in range(engine.global_steps, args.steps):
        engine.train_batch(debug=args.debug, mem_log=args.mem_log)


if __name__ == '__main__':
    from project_pactum.core.base import setup_logging
    setup_logging()

    args = get_args()

    test_encoder(args)
