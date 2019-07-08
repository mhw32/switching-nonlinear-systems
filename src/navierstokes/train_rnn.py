import os
import sys
import numpy as np
from tqdm import tqdm

import torch
import torch.optim as optim
import torch.nn.functional as F
from src.navierstokes.flow import DATA_DIR
from src.navierstokes.models import RNNDiffEq
from src.navierstokes.utils import \
    spatial_coarsen, AverageMeter, save_checkpoint, MODEL_DIR


def mean_squared_error(pred, true):
    batch_size = pred.size(0)
    pred, true = pred.view(batch_size, -1), true.view(batch_size, -1)
    mse = torch.mean(torch.pow(pred - true, 2), dim=1)
    return torch.mean(mse)  # over batch size


def build_batch(array, batch_start, batch_T):
    batch = []
    batch_size = len(batch_start)
    for i in range(batch_size):
        batch_i = array[batch_start[i]:batch_start[i]+batch_T]
        batch.append(batch_i)
    batch = np.stack(batch)
    return batch


def numpy_to_torch(array, device):
    return torch.from_numpy(array).float().to(device)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--test-only', action='store_true', default=False)
    args = parser.parse_args()

    use_cuda = torch.cuda.is_available()
    device = torch.device("cuda" if use_cuda else "cpu")

    torch.manual_seed(1337)
    np.random.seed(1337)

    os.makedirs(MODEL_DIR, exist_ok=True)

    batch_T = 50  # number of timesteps to sample
    batch_size = 100
    epochs = 2000

    dataset = np.load(os.path.join(DATA_DIR, 'data_nx_50_ny_50_dt_0.001.npz'))
    X, Y, u_seq, v_seq, p_seq = (dataset['X'], dataset['Y'], dataset['u'], 
                                 dataset['v'], dataset['p'])
    X, Y, u_seq, v_seq, p_seq = spatial_coarsen(X, Y, u_seq, v_seq, p_seq,
                                                agg_x=5, agg_y=5)

    T = u_seq.shape[0]

    u_in, v_in, p_in = u_seq[:T-1], v_seq[:T-1], p_seq[:T-1]
    u_out, v_out, p_out = u_seq[1:], v_seq[1:], p_seq[1:]

    model = RNNDiffEq(10)
    model = model.to(device)

    optimizer = optim.Adam(model.parameters(), lr=1e-3)
    best_loss = np.inf
    test_loss_item = np.inf

    if not args.test_only:
        pbar = tqdm(total=epochs)
        for iteration in range(epochs):
            model.train()
            # sample a batch of contiguous timesteps
            start = np.random.choice(np.arange(T - 1 - batch_T), size=batch_size)
            batch_u_in = numpy_to_torch(build_batch(u_in, start, batch_T), device)
            batch_v_in = numpy_to_torch(build_batch(v_in, start, batch_T), device)
            batch_p_in = numpy_to_torch(build_batch(p_in, start, batch_T), device)
            batch_u_out = numpy_to_torch(build_batch(u_out, start, batch_T), device)
            batch_v_out = numpy_to_torch(build_batch(v_out, start, batch_T), device)
            batch_p_out = numpy_to_torch(build_batch(p_out, start, batch_T), device)

            optimizer.zero_grad()
            batch_u_pred, batch_v_pred, batch_p_pred, _ = model(
                batch_u_in, batch_v_in, batch_p_in)
            loss = (mean_squared_error(batch_u_pred, batch_u_out) + 
                    mean_squared_error(batch_v_pred, batch_v_out) + 
                    mean_squared_error(batch_p_pred, batch_p_out))
            loss.backward()
            optimizer.step()
            pbar.update() 
            pbar.set_postfix({'train loss': loss.item(),
                              'test_loss': test_loss_item})

            if iteration % 10 == 0:
                model.eval()
                with torch.no_grad():
                    # test on entire dataset as test metric
                    test_u_in, test_u_out = numpy_to_torch(u_in, device), numpy_to_torch(u_out, device)
                    test_v_in, test_v_out = numpy_to_torch(v_in, device), numpy_to_torch(v_out, device)
                    test_p_in, test_p_out = numpy_to_torch(p_in, device), numpy_to_torch(p_out, device)
                    test_u_in, test_u_out = test_u_in.unsqueeze(0), test_u_out.unsqueeze(0)
                    test_v_in, test_v_out = test_v_in.unsqueeze(0), test_v_out.unsqueeze(0)
                    test_p_in, test_p_out = test_p_in.unsqueeze(0), test_p_out.unsqueeze(0)
                    test_u_pred, test_v_pred, test_p_pred, _ = model(
                        test_u_in, test_v_in, test_p_in)

                    test_loss = (mean_squared_error(test_u_pred, test_u_out) + 
                                 mean_squared_error(test_v_pred, test_v_out) + 
                                 mean_squared_error(test_p_pred, test_p_out))
                    test_loss_item = test_loss.item()
                    pbar.set_postfix({'train loss': loss.item(),
                                      'test_loss': test_loss_item}) 

                    if test_loss.item() < best_loss:
                        best_loss = test_loss.item()
                        is_best = True

                    save_checkpoint({
                        'state_dict': model.state_dict(),
                        'test_loss': test_loss.item(),
                    }, is_best, MODEL_DIR)    

        pbar.close()

    # load the best model
    checkpoint = torch.load(os.path.join(MODEL_DIR, 'model_best.pth.tar'))
    model.load_state_dict(checkpoint['state_dict'])
    model = model.eval()

    with torch.no_grad():
        # load originally coarse data (no artificial coarsening)
        dataset = np.load(os.path.join(DATA_DIR, 'data_nx_10_ny_10_dt_0.001.npz'))
        X, Y, _u_seq, _v_seq, _p_seq = (dataset['X'], dataset['Y'], dataset['u'], 
                                        dataset['v'], dataset['p'])
        u, v, p = _u_seq[0].copy(), _v_seq[0].copy(), _p_seq[0].copy()
        u, v, p = numpy_to_torch(u, device), numpy_to_torch(v, device), \
                  numpy_to_torch(p, device)
        u = u.unsqueeze(0).unsqueeze(0) 
        v = v.unsqueeze(0).unsqueeze(0)
        p = p.unsqueeze(0).unsqueeze(0)
        u_seq, v_seq, p_seq = [u.cpu().numpy()], [v.cpu().numpy()], [p.cpu().numpy()]
        h0 = None

        for _ in range(T - 1):
            u, v, p, h0 = model(u, v, p, rnn_h0=h0)
            u_seq.append(u.cpu().numpy())
            v_seq.append(v.cpu().numpy())
            p_seq.append(p.cpu().numpy())
        
        u_seq = np.stack(u_seq)
        v_seq = np.stack(v_seq)
        p_seq = np.stack(p_seq)

        np.savez(os.path.join(MODEL_DIR, 'rnn_nx_10_ny_10_dt_0.001.npz'),
                 X=X, Y=Y, u=u_seq, v=v_seq, p=p_seq)