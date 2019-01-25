from __future__ import division
from __future__ import print_function
from __future__ import absolute_import

import os
import sys
import numpy as np
from tqdm import tqdm

import torch
import torch.nn as nn
from torch import optim
import torch.nn.functional as F
from torch.optim import lr_scheduler

from datasets import BernoulliLorenz
from elbo import ldm_evidence_lower_bound
from ldm import LDM
from utils import AverageMeter


if __name__ == "__main__":

    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--out-dir', type=str, default='./')
    parser.add_argument('--seed', type=int, default=1)
    parser.add_argument('--cuda', action='store_true', default=False,
                        help='enables CUDA training')
    args = parser.parse_args()
    args.cuda = args.cuda and torch.cuda.is_available()
    device = torch.device('cuda' if args.cuda else 'cpu')

    # reproducibility
    torch.manual_seed(args.seed)
    np.random.seed(args.seed)

    train_dataset = BernoulliLorenz(100, 1000, dt=0.01)
    train_loader = torch.utils.data.DataLoader(train_dataset, batch_size=10, shuffle=True)

    model = LDM(100, 10, 100, 10, 20)
    model = model.to(device)
    
    optimizer = optim.Adam(model.parameters(), lr=1e-5)

    step = 0
    best_elbo = 0
    elbo_meter = AverageMeter()

    for i in xrange(10):
        for batch_idx, data in enumerate(train_loader):
            batch_size = len(data)
            data = data.to(device)

            output = model(data)
            elbo = ldm_evidence_lower_bound(data, output)
            elbo_meter.update(elbo.item(), batch_size)

            optimizer.zero_grad()
            elbo.backward()
            optimizer.step()

            if step % 1 == 0:
                print('step %d: loss = %.4f' % (step, elbo_meter.avg))

            if elbo.item() < best_elbo:
                best_elbo = elbo.item()
                torch.save(model.state_dict(), './params.pt')

            step += 1
