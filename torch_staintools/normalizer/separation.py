"""Note that some of the codes are derived from torchvahadane and staintools

"""
from typing import Callable

import torch

from torch_staintools.functional.optimization.dict_learning import get_concentrations
from torch_staintools.functional.stain_extraction.factory import build_from_name
from torch_staintools.functional.stain_extraction.utils import percentile
from .base import Normalizer


class StainSeperation(Normalizer):
    """Stain Seperation-based normalizer's interface: Macenko and Vahadane

    """
    get_stain_matrix: Callable
    stain_matrix_target: torch.Tensor
    target_concentrations: torch.Tensor

    def __init__(self, get_stain_matrix: Callable, reconst_method: str = 'ista'):
        """Init

        Args:
            get_stain_matrix: the Callable to obtain stain matrix - e.g., Vahadane's dict learning or
                macenko's SVD
            reconst_method:  How to get stain concentration from stain matrix

        """
        super().__init__()
        self.reconst_method = reconst_method
        self.get_stain_matrix = get_stain_matrix

    @staticmethod
    def _transpose_trailing(mat: torch.Tensor):
        """Helper function to transpose the trailing dimension, since data is batchified.

        Args:
            mat: input tensor ixjxk

        Returns:
            output with flipped dimension from ixjxk --> ixkxj
        """
        return torch.einsum("ijk -> ikj", mat)

    def fit(self, target, regularizer: float = 0.01, *args, **kwargs):
        """Fit to a target image.

        Note that the stain matrices are registered into buffers so that it's move to specified device
        along with the nn.Module object.

        Args:
            target: BCHW. Assume it's cast to torch.float32 and scaled to [0, 1]
            regularizer: Regularizer term in dict learning. Note that similar to staintools, for image
                reconstruction step, we also use dictionary learning to get the target stain concentration.
            *args: Positional argument of stain seperator (get_stain_matrix)
            **kwargs: Keyword argument of stain seperator.

        Returns:

        """
        assert target.shape[0] == 1
        stain_matrix_target = self.get_stain_matrix(target, *args, **kwargs)

        self.register_buffer('stain_matrix_target', stain_matrix_target)
        target_conc = get_concentrations(target, self.stain_matrix_target, regularizer=regularizer,
                                         algorithm=self.reconst_method)
        self.register_buffer('target_concentrations', target_conc)
        # B x (HW) x 2
        conc_transpose = StainSeperation._transpose_trailing(self.target_concentrations)
        # along HW dim
        max_c_target = percentile(conc_transpose, 99, dim=1)
        self.register_buffer('maxC_target', max_c_target)
        # self.maxC_target = np.percentile(self.target_concentrations, 99, axis=0).reshape((1, 2))

    def fix_source_stain_matrix(self, image, *args, **kwargs):
        """there could also be a way of fixing a target matrix.

        If a set of transformations are done on same wsi, stain matrix should not meaningfully change over time,
        have a moving average and if average converges to new samples then set as matrix?
        """

        self.register_buffer('stain_matrix_source', self.get_stain_matrix(image, *args, **kwargs))

    @staticmethod
    def repeat_stain_mat(stain_mat: torch.Tensor, image: torch.Tensor) -> torch.Tensor:
        """Helper function for vectorization and broadcasting

        Args:
            stain_mat: a (usually source) stain matrix obtained from fitting
            image: input batch image

        Returns:
            repeated stain matrix
        """
        stain_mat = torch.atleast_3d(stain_mat)
        repeat_dim = (image.shape[0],) + (1,) * (stain_mat.ndimension() - 1)
        return stain_mat.repeat(*repeat_dim)

    def transform(self, image: torch.Tensor, *args, **kwargs):
        """Transformation operation.


        Stain matrix is extracted from source image use specified stain seperator (dict learning or svd)

        Target concentration is by default computed by dict learning for both macenko and vahadane, same as
        staintools.

        Normalize the concentration and reconstruct image to OD.

        Args:
            image: Image input must be BxCxHxW cast to torch.float32 and rescaled to [0, 1]
                Check torchvision.transforms.convert_image_dtype.
            *args: Positional argument of stain seperator (get_stain_matrix)
            **kwargs: Keyword argument of stain seperator.
        Returns:
            torch.Tensor: normalized output in BxCxHxW and float32. Note that some pixel value may exceed [0, 1] and
            must be scaled/clamped by torchvision.transforms.convert_image_dtype.
        """
        # one source matrix - multiple target
        if not hasattr(self, 'stain_matrix_source'):
            stain_matrix_source = self.get_stain_matrix(image, *args, **kwargs)
        else:
            stain_matrix_source = self.stain_matrix_source
        # stain_matrix_source -- B x 2 x 3 wherein B is 1. Note that the input batch size is independent of how many
        # template were used and for now we only accept one template a time. todo - multiple template for sampling later
        stain_matrix_source: torch.Tensor
        stain_matrix_source = StainSeperation.repeat_stain_mat(stain_matrix_source, image)
        # B * 2 * (HW)
        source_concentration = get_concentrations(image, stain_matrix_source, algorithm=self.reconst_method)
        # individual shape (2,) (HE)
        c_transposed_srd = StainSeperation._transpose_trailing(source_concentration)
        maxC = percentile(c_transposed_srd, 99, dim=1)
        # 1 x B x 2
        c_scale = (self.maxC_target / maxC).unsqueeze(-1)
        source_concentration *= c_scale
        # note this is the reconstruction in B x (HW) x C --> need to shuffle the channel first before reshape
        out = torch.exp(-1 * torch.matmul(c_transposed_srd, self.stain_matrix_target))
        out = StainSeperation._transpose_trailing(out)
        return out.reshape(image.shape).clamp_(0, 1)

    def forward(self, x: torch.Tensor, *args, **kwargs):
        return self.transform(x, *args, **kwargs)

    @classmethod
    def build(cls, method: str, *args, **kwargs) -> "StainSeperation":
        """Builder.

        Args:
            method: method of stain extractor name: vadahane or macenko
            *args: Positional argument of stain seperator (get_stain_matrix)
            **kwargs: Keyword argument of stain seperator.

        Returns:
            StainSeperation normalizer.
        """
        method = method.lower()
        extractor = build_from_name(method)
        return cls(extractor, *args, **kwargs)