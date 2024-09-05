
# Copyright 2023 The EASYDEL Author @erfanzar (Erfan Zare Chavoshi).
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import jax
from jax import lax
from jax import numpy as jnp

from easydel.kernels.matmul import matmul_kernel


def grok1_mlp_pallas(
	x,
	linear,
	linear_1,
	linear_v,
	*,
	blocksize_m: int = 16,
	blocksize_k: int = 64,
	blocksize_n: int = 16,
	prod_dtype: jnp.dtype = jnp.float32,
	precision: lax.PrecisionLike = None,
):
	args = dict(
		blocksize_k=blocksize_k,
		blocksize_m=blocksize_m,
		blocksize_n=blocksize_n,
		prod_dtype=prod_dtype,
		precision=precision,
	)
	

	return matmul_kernel(
		(
			jax.nn.gelu(matmul_kernel(x, linear, **args)) * matmul_kernel(x, linear_v, **args)
		),
		linear_1,
		**args,
	)
