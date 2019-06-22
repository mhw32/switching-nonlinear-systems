import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Function 
from torch.autograd.function import once_differentiable
from torch.distributions import constraints, Categorical

from pyro.distributions.torch_distribution import TorchDistribution
from pyro.distributions.util import sum_leftmost

import math
import numpy as np


def log_normal_pdf(x, mean, logvar):
    const = torch.from_numpy(np.array([2. * np.pi])).float().to(x.device)
    const = torch.log(const)
    return -.5 * (const + logvar + (x - mean) ** 2. / torch.exp(logvar))


def normal_kl(mu1, lv1, mu2, lv2):
    v1 = torch.exp(lv1)
    v2 = torch.exp(lv2)
    lstd1 = lv1 / 2.
    lstd2 = lv2 / 2.

    kl = lstd2 - lstd1 + ((v1 + (mu1 - mu2) ** 2.) / (2. * v2)) - .5
    return kl


def log_mean_exp(x, dim=1):
    r"""log(1/k * sum(exp(x))): this normalizes x.

    @param x: PyTorch.Tensor
              samples from gaussian
    @param dim: integer (default: 1)
                which dimension to take the mean over
    @return: PyTorch.Tensor
             mean of x
    """
    m = torch.max(x, dim=dim, keepdim=True)[0]
    return m + torch.log(torch.mean(torch.exp(x - m),
                         dim=dim, keepdim=True))


class AverageMeter(object):
    """Computes and stores the average and current value"""
    def __init__(self):
        self.reset()

    def reset(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0

    def update(self, val, n=1):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count


class Sparsemax(nn.Module):
    """Sparsemax function."""

    def __init__(self, dim=None):
        """Initialize sparsemax activation
        Args:
            dim (int, optional): The dimension over which to apply the sparsemax function.
        """
        super(Sparsemax, self).__init__()

        self.dim = -1 if dim is None else dim

    def forward(self, input):
        """Forward function.
        Args:
            input (torch.Tensor): Input tensor. First dimension should be the batch size
        Returns:
            torch.Tensor: [batch_size x number_of_logits] Output tensor
        """
        # Sparsemax currently only handles 2-dim tensors,
        # so we reshape and reshape back after sparsemax
        original_size = input.size()
        input = input.view(-1, input.size(self.dim))

        dim = 1
        number_of_logits = input.size(dim)

        # Translate input by max for numerical stability
        input = input - torch.max(input, dim=dim, keepdim=True)[0].expand_as(input)

        # Sort input in descending order.
        # (NOTE: Can be replaced with linear time selection method described here:
        # http://stanford.edu/~jduchi/projects/DuchiShSiCh08.html)
        zs = torch.sort(input=input, dim=dim, descending=True)[0]
        range = torch.arange(start=1, end=number_of_logits+1, device=input.device).view(1, -1)
        range = range.expand_as(zs).float()

        # Determine sparsity of projection
        bound = 1 + range * zs
        cumulative_sum_zs = torch.cumsum(zs, dim)
        is_gt = torch.gt(bound, cumulative_sum_zs).type(input.type())
        k = torch.max(is_gt * range, dim, keepdim=True)[0]

        # Compute threshold function
        zs_sparse = is_gt * zs

        # Compute taus
        taus = (torch.sum(zs_sparse, dim, keepdim=True) - 1) / k
        taus = taus.expand_as(input)

        # Sparsemax
        self.output = torch.max(torch.zeros_like(input), input - taus)

        output = self.output.view(original_size)

        return output

    def backward(self, grad_output):
        """Backward function."""
        dim = 1

        nonzeros = torch.ne(self.output, 0)
        sum = torch.sum(grad_output * nonzeros, dim=dim) / torch.sum(nonzeros, dim=dim)
        self.grad_input = nonzeros * (grad_output - sum.expand_as(grad_output))

        return self.grad_input


class MixtureOfDiagNormals(TorchDistribution):
    """From pyro's public Github repo.

    Mixture of Normal distributions with arbitrary means and arbitrary
    diagonal covariance matrices.
    That is, this distribution is a mixture with K components, where each
    component distribution is a D-dimensional Normal distribution with a
    D-dimensional mean parameter and a D-dimensional diagonal covariance
    matrix. The K different component means are gathered into the K x D
    dimensional parameter `locs` and the K different scale parameters are
    gathered into the K x D dimensional parameter `coord_scale`. The mixture
    weights are controlled by a K-dimensional vector of softmax logits,
    `component_logits`. This distribution implements pathwise derivatives
    for samples from the distribution.
    See reference [1] for details on the implementations of the pathwise
    derivative. Please consider citing this reference if you use the pathwise
    derivative in your research. Note that this distribution does not support
    dimension D = 1.
    [1] Pathwise Derivatives for Multivariate Distributions, Martin Jankowiak &
    Theofanis Karaletsos. arXiv:1806.01856
    :param torch.Tensor locs: K x D mean matrix
    :param torch.Tensor coord_scale: K x D scale matrix
    :param torch.Tensor component_logits: K-dimensional vector of softmax logits
    """
    has_rsample = True
    arg_constraints = {"locs": constraints.real, "coord_scale": constraints.positive,
                       "component_logits": constraints.real}

    def __init__(self, locs, coord_scale, component_logits):
        self.batch_mode = (locs.dim() > 2)
        assert(coord_scale.shape == locs.shape)
        assert(self.batch_mode or locs.dim() == 2), \
            "The locs parameter in MixtureOfDiagNormals should be K x D dimensional (or B x K x D if doing batches)"
        if not self.batch_mode:
            assert(coord_scale.dim() == 2), \
                "The coord_scale parameter in MixtureOfDiagNormals should be K x D dimensional"
            assert(component_logits.dim() == 1), \
                "The component_logits parameter in MixtureOfDiagNormals should be K dimensional"
            assert(component_logits.size(-1) == locs.size(-2))
            batch_shape = ()
        else:
            assert(coord_scale.dim() > 2), \
                "The coord_scale parameter in MixtureOfDiagNormals should be B x K x D dimensional"
            assert(component_logits.dim() > 1), \
                "The component_logits parameter in MixtureOfDiagNormals should be B x K dimensional"
            assert(component_logits.size(-1) == locs.size(-2))
            batch_shape = tuple(locs.shape[:-2])

        self.locs = locs
        self.coord_scale = coord_scale
        self.component_logits = component_logits
        self.dim = locs.size(-1)
        self.categorical = Categorical(logits=component_logits)
        self.probs = self.categorical.probs
        super(MixtureOfDiagNormals, self).__init__(batch_shape=torch.Size(batch_shape),
                                                   event_shape=torch.Size((self.dim,)))

    def expand(self, batch_shape, _instance=None):
        new = self._get_checked_instance(MixtureOfDiagNormals, _instance)
        new.batch_mode = True
        batch_shape = torch.Size(batch_shape)
        new.dim = self.dim
        new.locs = self.locs.expand(batch_shape + self.locs.shape[-2:])
        new.coord_scale = self.coord_scale.expand(batch_shape + self.coord_scale.shape[-2:])
        new.component_logits = self.component_logits.expand(batch_shape + self.component_logits.shape[-1:])
        new.categorical = self.categorical.expand(batch_shape)
        new.probs = self.probs.expand(batch_shape + self.probs.shape[-1:])
        super(MixtureOfDiagNormals, new).__init__(batch_shape, self.event_shape, validate_args=False)
        new._validate_args = self._validate_args
        return new

    def log_prob(self, value):
        epsilon = (value.unsqueeze(-2) - self.locs) / self.coord_scale  # L B K D
        eps_sqr = 0.5 * torch.pow(epsilon, 2.0).sum(-1)  # L B K
        eps_sqr_min = torch.min(eps_sqr, -1)[0]  # L B K
        coord_scale_prod = self.coord_scale.log().sum(-1).exp()  # B K
        result = self.probs * torch.exp(-eps_sqr + eps_sqr_min.unsqueeze(-1)) / coord_scale_prod  # L B K
        result = torch.log(result.sum(-1))  # L B
        result = result - 0.5 * math.log(2.0 * math.pi) * float(self.dim)
        result = result - eps_sqr_min
        return result

    def rsample(self, sample_shape=torch.Size()):
        which = self.categorical.sample(sample_shape)
        return _MixDiagNormalSample.apply(self.locs, self.coord_scale,
                                          self.component_logits, self.categorical.probs, which,
                                          sample_shape + self.locs.shape[:-2] + (self.dim,))


class _MixDiagNormalSample(Function):
    @staticmethod
    def forward(ctx, locs, scales, component_logits, pis, which, noise_shape):
        dim = scales.size(-1)
        white = locs.new(noise_shape).normal_()
        n_unsqueezes = locs.dim() - which.dim()
        for _ in range(n_unsqueezes):
            which = which.unsqueeze(-1)
        which_expand = which.expand(tuple(which.shape[:-1] + (dim,)))
        loc = torch.gather(locs, -2, which_expand).squeeze(-2)
        sigma = torch.gather(scales, -2, which_expand).squeeze(-2)
        z = loc + sigma * white
        ctx.save_for_backward(z, scales, locs, component_logits, pis)
        return z

    @staticmethod
    @once_differentiable
    def backward(ctx, grad_output):

        z, scales, locs, logits, pis = ctx.saved_tensors
        dim = scales.size(-1)
        K = logits.size(-1)
        g = grad_output  # l b i
        g = g.unsqueeze(-2)  # l b 1 i
        batch_dims = locs.dim() - 2

        locs_tilde = locs / scales  # b j i
        sigma_0 = torch.min(scales, -2, keepdim=True)[0]  # b 1 i
        z_shift = (z.unsqueeze(-2) - locs) / sigma_0  # l b j i
        z_tilde = z.unsqueeze(-2) / scales - locs_tilde  # l b j i

        mu_cd = locs.unsqueeze(-2) - locs.unsqueeze(-3)  # b c d i
        mu_cd_norm = torch.pow(mu_cd, 2.0).sum(-1).sqrt()  # b c d
        mu_cd /= mu_cd_norm.unsqueeze(-1)  # b c d i
        diagonals = torch.empty((K,), dtype=torch.long, device=z.device)
        torch.arange(K, out=diagonals)
        mu_cd[..., diagonals, diagonals, :] = 0.0

        mu_ll_cd = (locs.unsqueeze(-2) * mu_cd).sum(-1)  # b c d
        z_ll_cd = (z.unsqueeze(-2).unsqueeze(-2) * mu_cd).sum(-1)  # l b c d
        z_perp_cd = z.unsqueeze(-2).unsqueeze(-2) - z_ll_cd.unsqueeze(-1) * mu_cd  # l b c d i
        z_perp_cd_sqr = torch.pow(z_perp_cd, 2.0).sum(-1)  # l b c d

        shift_indices = torch.empty((dim,), dtype=torch.long, device=z.device)
        torch.arange(dim, out=shift_indices)
        shift_indices = shift_indices - 1
        shift_indices[0] = 0

        z_shift_cumsum = torch.pow(z_shift, 2.0)
        z_shift_cumsum = z_shift_cumsum.sum(-1, keepdim=True) - torch.cumsum(z_shift_cumsum, dim=-1)  # l b j i
        z_tilde_cumsum = torch.cumsum(torch.pow(z_tilde, 2.0), dim=-1)  # l b j i
        z_tilde_cumsum = torch.index_select(z_tilde_cumsum, -1, shift_indices)
        z_tilde_cumsum[..., 0] = 0.0
        r_sqr_ji = z_shift_cumsum + z_tilde_cumsum  # l b j i

        log_scales = torch.log(scales)  # b j i
        epsilons_sqr = torch.pow(z_tilde, 2.0)  # l b j i
        log_qs = -0.5 * epsilons_sqr - 0.5 * math.log(2.0 * math.pi) - log_scales  # l b j i
        log_q_j = log_qs.sum(-1, keepdim=True)  # l b j 1
        q_j = torch.exp(log_q_j)  # l b j 1
        q_tot = (pis * q_j.squeeze(-1)).sum(-1)  # l b
        q_tot = q_tot.unsqueeze(-1)  # l b 1

        root_two = math.sqrt(2.0)
        shift_log_scales = log_scales[..., shift_indices]
        shift_log_scales[..., 0] = 0.0
        sigma_products = torch.cumsum(shift_log_scales, dim=-1).exp()  # b j i

        reverse_indices = torch.tensor(range(dim - 1, -1, -1), dtype=torch.long, device=z.device)
        reverse_log_sigma_0 = sigma_0.log()[..., reverse_indices]  # b 1 i
        sigma_0_products = torch.cumsum(reverse_log_sigma_0, dim=-1).exp()[..., reverse_indices - 1]  # b 1 i
        sigma_0_products[..., -1] = 1.0
        sigma_products *= sigma_0_products

        logits_grad = torch.erf(z_tilde / root_two) - torch.erf(z_shift / root_two)  # l b j i
        logits_grad *= torch.exp(-0.5 * r_sqr_ji)  # l b j i
        logits_grad = (logits_grad * g / sigma_products).sum(-1)  # l b j
        logits_grad = sum_leftmost(logits_grad / q_tot, -1 - batch_dims)  # b j
        logits_grad *= 0.5 * math.pow(2.0 * math.pi, -0.5 * (dim - 1))
        logits_grad = -pis * logits_grad
        logits_grad = logits_grad - logits_grad.sum(-1, keepdim=True) * pis

        mu_ll_dc = torch.transpose(mu_ll_cd, -1, -2)
        v_cd = torch.erf((z_ll_cd - mu_ll_cd) / root_two) - torch.erf((z_ll_cd + mu_ll_dc) / root_two)
        v_cd *= torch.exp(-0.5 * z_perp_cd_sqr)  # l b c d
        mu_cd_g = (g.unsqueeze(-2) * mu_cd).sum(-1)  # l b c d
        v_cd *= -mu_cd_g * pis.unsqueeze(-2) * 0.5 * math.pow(2.0 * math.pi, -0.5 * (dim - 1))  # l b c d
        v_cd = pis * sum_leftmost(v_cd.sum(-1) / q_tot, -1 - batch_dims)
        logits_grad += v_cd

        prefactor = pis.unsqueeze(-1) * q_j * g / q_tot.unsqueeze(-1)
        locs_grad = sum_leftmost(prefactor, -2 - batch_dims)
        scales_grad = sum_leftmost(prefactor * z_tilde, -2 - batch_dims)

        return locs_grad, scales_grad, logits_grad, None, None, None


def sample_gumbel(shape, eps=1e-20):
    U = torch.rand(shape).cuda()
    return -torch.log(-torch.log(U + eps) + eps)


def gumbel_softmax_sample(logits, temperature):
    y = logits + sample_gumbel(logits.size())
    return F.softmax(y / temperature, dim=-1)


def gumbel_softmax(logits, temperature):
    r"""ST-gumple-softmax

    input: [*, n_class]
    return: flatten --> [*, n_class] an one-hot vector
    """
    y = gumbel_softmax_sample(logits, temperature)
    shape = y.size()
    _, ind = y.max(dim=-1)
    y_hard = torch.zeros_like(y).view(-1, shape[-1])
    y_hard.scatter_(1, ind.view(-1, 1), 1)
    y_hard = y_hard.view(*shape)
    y_hard = (y_hard - y).detach() + y
    return y_hard.view(-1, shape[1] * shape[2])
