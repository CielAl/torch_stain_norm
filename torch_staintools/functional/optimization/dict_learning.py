"""
code directly adapted from https://github.com/rfeinman/pytorch-lasso
"""
import warnings
from .solver import coord_descent, ista
from .sparse_util import initialize_code
from torch_staintools.functional.conversion.od import rgb2od
from tqdm import tqdm
import torch
import torch.nn as nn
import torch.nn.functional as F
from ..eps import get_eps
# min_{D in C} = (1/n) sum_{i=1}^n (1/2)||x_i-Dalpha_i||_2^2 + lambda1||alpha_i||_1 + lambda1_2||alpha_i||_2^2

_init_defaults = {
    'ista': 'zero',
    'cd': 'zero',
}


def lasso_loss(X, Z, weight, alpha=1.0):
    X_hat = torch.matmul(Z, weight.T)
    lambda2 = 10e-10
    lambda1 = 0.1
    loss = 0.5 * (X - X_hat).norm(p=2).pow(2) + weight.norm(p=1) * lambda1 + lambda2 * weight.norm(p=2).pow(2)
    return loss.mean()


def update_dict(dictionary: torch.Tensor, x: torch.Tensor, code: torch.Tensor,
                positive=True,
                eps=1e-7, rng: torch.Generator = None):
    """
    Update the dense dictionary factor in place.

    Modified from `_update_dict` in sklearn.decomposition._dict_learning


    Args:
        dictionary:  Tensor of shape (n_features, n_components) Value of the dictionary at the previous iteration.
        x: Tensor of shape (n_samples, n_components)
            Sparse coding of the data against which to optimize the dictionary.
        code:  Tensor of shape (n_samples, n_components)
            Sparse coding of the data against which to optimize the dictionary.
        positive: Whether to enforce positivity when finding the dictionary.
        eps: Minimum vector norm before considering "degenerate"
        rng: torch.Generator for initialization of dictionary and code.

    Returns:

    """
    n_components = dictionary.size(1)

    # Residuals
    R = x - torch.matmul(code, dictionary.T)  # (n_samples, n_features)
    for k in range(n_components):
        # Update k'th atom
        R += torch.outer(code[:, k], dictionary[:, k])
        dictionary[:, k] = torch.matmul(code[:, k], R)
        if positive:
            dictionary[:, k].clamp_(0, None)

        # Re-scale k'th atom
        atom_norm = dictionary[:, k].norm()
        if atom_norm < eps:
            dictionary[:, k].normal_(generator=rng)

            if positive:
                # if all negative then the clamped output will be zero vector.
                # the division of zero vector to zero-norm --> nan
                dictionary[:, k].clamp_(0, None)
                dictionary[:, k] = dictionary[:, k].abs()
            # another layer of protection
            column_norm = dictionary[:, k].norm() + get_eps(dictionary)
            dictionary[:, k] /= column_norm  # dictionary[:, k].norm()
            # Set corresponding coefs to 0
            code[:, k].zero_()  # TODO: is this necessary?
        else:
            dictionary[:, k] /= atom_norm
            R -= torch.outer(code[:, k], dictionary[:, k])
    return dictionary


def update_dict_ridge(x, code, lambd=1e-4):
    """Update an (unconstrained) dictionary with ridge regression

    This is equivalent to a Newton step with the (L2-regularized) squared
    error objective:
    f(V) = (1/2N) * ||Vz - x||_2^2 + (lambd/2) * ||V||_2^2

    Args:
        x:  a batch of observations with shape (n_samples, n_features)
        code: (z) a batch of code vectors with shape (n_samples, n_components)
        lambd:  weight decay parameter

    Returns:

    """

    rhs = torch.mm(code.T, x)
    M = torch.mm(code.T, code)
    M.diagonal().add_(lambd * x.size(0))
    L = torch.linalg.cholesky(M)
    V = torch.cholesky_solve(rhs, L).T

    return V


def dict_evaluate(x: torch.Tensor, weight: torch.Tensor, alpha: float, rng: torch.Generator, **kwargs):
    x = x.to(weight.device)
    Z = sparse_encode(x, weight, alpha, rng=rng, **kwargs)
    loss = lasso_loss(x, Z, weight, alpha)
    return loss


def sparse_encode(x: torch.Tensor, weight: torch.Tensor, alpha: float = 0.1,
                  z0=None, algorithm='ista', init=None, rng: torch.Generator = None,
                  **kwargs):
    n_samples = x.size(0)
    n_components = weight.size(1)

    # initialize code variable
    if z0 is not None:
        assert z0.shape == (n_samples, n_components)
    else:
        if init is None:
            init = _init_defaults.get(algorithm, 'zero')
        elif init == 'zero' and algorithm == 'iter-ridge':
            warnings.warn("Iterative Ridge should not be zero-initialized.")
        z0 = initialize_code(x, weight, alpha, mode=init, rng=rng)

    # perform inference
    match algorithm:
        case 'cd':
            z = coord_descent(x, weight, z0, alpha, **kwargs)
        case 'ista':
            z = ista(x, z0, weight, alpha, rng=rng, **kwargs)
        case _:
            raise ValueError("invalid algorithm parameter '{}'.".format(algorithm))
    return z


def dict_learning(x, n_components, *, alpha=1.0, constrained=True, persist=False,
                  lambd=1e-2, steps=60, device='cpu', progbar=True, rng: torch.Generator = None,
                  **solver_kwargs):
    n_samples, n_features = x.shape
    x = x.to(device)

    weight = torch.randn(n_features, n_components, device=device, generator=rng)
    nn.init.orthogonal_(weight)
    # l2(w)
    if constrained:
        weight = F.normalize(weight, dim=0)
    Z0 = None

    losses = torch.zeros(steps, device=device)
    with tqdm(total=steps, disable=not progbar) as progress_bar:
        for i in range(steps):
            # infer sparse coefficients and compute loss

            Z = sparse_encode(x, weight, alpha, Z0, rng=rng, **solver_kwargs)
            losses[i] = lasso_loss(x, Z, weight, alpha)
            if persist:
                Z0 = Z

            # update dictionary
            if constrained:
                weight = update_dict(weight, x, Z, positive=True, rng=rng)
            else:
                weight = update_dict_ridge(x, Z, lambd=lambd)

            # update progress bar
            progress_bar.set_postfix(loss=losses[i].item())
            progress_bar.update(1)

    return weight, losses


def get_concentrations_helper(od_flatten, stain_matrix, regularizer=0.01, method='ista', rng: torch.Generator = None):
    """Helper function to estimate concentration matrix given an image and stain matrix with shape: 2 x (H*W)

    Args:
        od_flatten: Flattened optical density vectors in shape of B x (H*W) x C (H and W dimensions flattened).
        stain_matrix: the computed stain matrices in shape of B x num_stain x input channel
        regularizer: regularization term if ISTA algorithm is used
        method: which method to compute the concentration: coordinate descent ('cd') or iterative-shrinkage soft
            thresholding algorithm ('ista')
        rng: torch.Generator for random initializations
    Returns:
        computed concentration: B x num_stains x num_pixel_in_tissue_mask
    """

    match method:
        case 'cd':
            return coord_descent(od_flatten, stain_matrix.T, alpha=regularizer).T  # figure out pylasso equivalent
        case 'ista':
            return ista(od_flatten, 'ridge', stain_matrix.T, alpha=regularizer, rng=rng).T

    raise NotImplementedError(f"{method} is not a valid optimizer")


def get_concentrations(image, stain_matrix, regularizer=0.01, algorithm='ista', rng: torch.Generator = None):
    """Estimate concentration matrix given an image and stain matrix.

    Args:
        image: batched image(s) in shape of BxCxHxW
        stain_matrix: B x num_stain x input channel
        regularizer: regularization term if ISTA algorithm is used
        algorithm: which method to compute the concentration: coordinate descent ('cd') or iterative-shrinkage soft
            thresholding algorithm ('ista')
        rng: torch.Generator for random initializations
    Returns:
        concentration matrix: B x num_stains x num_pixel_in_tissue_mask
    """
    device = image.device
    stain_matrix = stain_matrix.to(device)
    # BCHW
    od = rgb2od(image).to(device)
    # B (H*W) C
    od_flatten = od.flatten(start_dim=2, end_dim=-1).permute(0, 2, 1)
    result = list()
    for od_single, stain_mat_single in zip(od_flatten, stain_matrix):
        result.append(get_concentrations_helper(od_single, stain_mat_single, regularizer, algorithm, rng=rng))
    # get_concentrations_helper(od_flatten, stain_matrix, regularizer, method)
    return torch.stack(result)
