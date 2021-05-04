__copyright__ = """Copyright (C) 2018 Alexandru Fikl"""

__license__ = """
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""

import numpy as np

from pytools import keyed_memoize_method, keyed_memoize_in, memoize_in
from pytools.obj_array import obj_array_vectorized_n_args

import loopy as lp

from meshmode.array_context import make_loopy_program
from meshmode.dof_array import DOFArray
from meshmode.discretization.connection.direct import (
        DiscretizationConnection,
        DirectDiscretizationConnection)
from meshmode.discretization.connection.chained import \
        ChainedDiscretizationConnection


class L2ProjectionInverseDiscretizationConnection(DiscretizationConnection):
    """Creates an inverse :class:`DiscretizationConnection` from an existing
    connection to allow transporting from the original connection's
    *to_discr* to *from_discr*.

    .. attribute:: from_discr
    .. attribute:: to_discr
    .. attribute:: is_surjective

    .. attribute:: conn
    .. automethod:: __call__

    """

    def __new__(cls, connections, is_surjective=False):
        if isinstance(connections, DirectDiscretizationConnection):
            return DiscretizationConnection.__new__(cls)
        elif isinstance(connections, ChainedDiscretizationConnection):
            if len(connections.connections) == 0:
                return connections

            return cls(connections.connections, is_surjective=is_surjective)
        else:
            conns = []
            for cnx in reversed(connections):
                conns.append(cls(cnx, is_surjective=is_surjective))

            return ChainedDiscretizationConnection(conns)

    def __init__(self, conn, is_surjective=False):
        if conn.from_discr.dim != conn.to_discr.dim:
            raise RuntimeError("cannot transport from face to element")

        if not all(g.is_orthonormal_basis() for g in conn.to_discr.groups):
            raise RuntimeError("`to_discr` must have an orthonormal basis")

        self.conn = conn
        super().__init__(
                from_discr=self.conn.to_discr,
                to_discr=self.conn.from_discr,
                is_surjective=is_surjective)

    @keyed_memoize_method(key=lambda actx: ())
    def _batch_weights(self, actx):
        """Computes scaled quadrature weights for each interpolation batch in
        :attr:`conn`. The quadrature weights can be used to integrate over
        child elements in the domain of the parent element, by a change of
        variables.

        :return: a dictionary with keys ``(group_id, batch_id)``.
        """

        from pymbolic.geometric_algebra import MultiVector
        from functools import reduce
        from operator import xor

        def det(v):
            nnodes = v[0].shape[0]
            det_v = np.empty(nnodes)

            for i in range(nnodes):
                outer_product = reduce(xor, [MultiVector(x[i, :].T) for x in v])
                det_v[i] = abs((outer_product.I | outer_product).as_scalar())

            return det_v

        weights = {}
        jac = np.empty(self.to_discr.dim, dtype=object)

        from meshmode.discretization.poly_element import diff_matrices
        for igrp, grp in enumerate(self.to_discr.groups):
            matrices = diff_matrices(grp)

            for ibatch, batch in enumerate(self.conn.groups[igrp].batches):
                for iaxis in range(grp.dim):
                    jac[iaxis] = matrices[iaxis] @ batch.result_unit_nodes.T

                weights[igrp, ibatch] = actx.freeze(actx.from_numpy(
                    det(jac) * grp.weights))

        return weights

    @obj_array_vectorized_n_args
    def __call__(self, ary):
        if not isinstance(ary, DOFArray):
            raise TypeError("non-array passed to discretization connection")

        actx = ary.array_context

        @memoize_in(
            actx, (L2ProjectionInverseDiscretizationConnection,
                   "conn_projection_knl")
        )
        def kproj():
            return make_loopy_program(
                [
                    "{[iel]: 0 <= iel < nelements}",
                    "{[i_quad]: 0 <= i_quad < n_to_nodes}",
                    "{[ibasis]: 0 <= ibasis < n_to_nodes}"
                ],
                """
                    result[to_element_indices[iel], ibasis] =               \
                        result[to_element_indices[iel], ibasis] +           \
                        sum(i_quad, ary[from_element_indices[iel], i_quad]  \
                                    * basis_tabulation[ibasis, i_quad]      \
                                    * weights[i_quad])
                """,
                [
                    lp.GlobalArg("ary", None,
                                 shape=("n_from_elements", "n_from_nodes")),
                    lp.GlobalArg("result", None,
                                 shape=("n_to_elements", "n_to_nodes")),
                    lp.GlobalArg("basis_tabulation", None,
                                 shape=("n_to_nodes", "n_to_nodes")),
                    lp.GlobalArg("weights", None,
                                 shape="n_from_nodes"),
                    lp.ValueArg("n_from_elements", np.int32),
                    lp.ValueArg("n_from_nodes", np.int32),
                    lp.ValueArg("n_to_elements", np.int32),
                    lp.ValueArg("n_to_nodes", np.int32),
                    "..."
                ],
                name="conn_projection_knl"
            )

        # compute weights on each refinement of the reference element
        weights = self._batch_weights(actx)

        # perform dot product (on reference element) to get basis coefficients
        group_idx_to_c = []
        for igrp, cgrp in enumerate(self.conn.groups):
            kernels = []   # get kernels for each batch; to be fused eventually
            kwargs = {}    # kwargs to the fused kernel
            for ibatch, batch in enumerate(cgrp.batches):
                sgrp = self.from_discr.groups[batch.from_group_index]

                knl = lp.rename_argument(
                    kproj(), "basis_tabulation",
                    f"basis_tabulation_batch{ibatch}"
                )
                knl = lp.rename_argument(knl, "ary", f"ary_batch{ibatch}")
                knl = lp.rename_argument(knl, "weights", f"weights_batch{ibatch}")
                knl = lp.rename_argument(knl, "from_element_indices",
                                         f"from_element_indices_{ibatch}")
                knl = lp.rename_argument(knl, "to_element_indices",
                                         f"to_element_indices_{ibatch}")
                knl = lp.rename_argument(knl, "nelements",
                                         f"nelements_{ibatch}")

                for iname in knl.all_inames():
                    knl = lp.rename_iname(knl, iname, f"{iname}_batch{ibatch}")

                # Generate the basis tabulation matrix
                tabulations = []
                for ibasis, basis_fn in enumerate(sgrp.basis_obj().functions):
                    tabulations.append(basis_fn(batch.result_unit_nodes).flatten())
                tabulations = actx.from_numpy(np.asarray(tabulations))

                kwargs[f"basis_tabulation_batch{ibatch}"] = tabulations
                kwargs[f"ary_batch{ibatch}"] = ary[sgrp.index]
                kwargs[f"weights_batch{ibatch}"] = weights[igrp, ibatch]
                kwargs[f"from_element_indices_{ibatch}"] = \
                    batch.from_element_indices
                kwargs[f"to_element_indices_{ibatch}"] = \
                    batch.to_element_indices

                kernels.append(knl)

            fused_knl = lp.fuse_kernels(kernels)
            fused_knl = fused_knl.copy(name="fused_kproj_program")
            fused_knl = lp.add_nosync(fused_knl,
                                      "global",
                                      "writes:result",
                                      "writes:result",
                                      bidirectional=True,
                                      force=True)

            from meshmode.array_context import _DontTransformMeBro
            fused_knl = fused_knl.tagged(frozenset((_DontTransformMeBro(),)))

            result_dict = actx.call_loopy(
                fused_knl,
                n_to_elements=self.to_discr.groups[igrp].nelements,
                n_to_nodes=self.to_discr.groups[igrp].nunit_dofs,
                **kwargs
            )
            group_idx_to_c.append(result_dict["result"])

        c = DOFArray(actx, data=tuple(group_idx_to_c))

        @memoize_in(
            actx, (L2ProjectionInverseDiscretizationConnection,
                   "conn_evaluation_knl")
        )
        def keval():
            return make_loopy_program(
                [
                    "{[iel]: 0 <= iel < nelements}",
                    "{[idof]: 0 <= idof < n_to_nodes}",
                    "{[ibasis]: 0 <= ibasis < n_to_nodes}"
                ],
                """
                    result[iel, idof] =                \
                        sum(ibasis, vdm[idof, ibasis]  \
                                    * coefficients[iel, ibasis])
                """,
                [
                    lp.GlobalArg("coefficients", None,
                                 shape=("nelements", "n_to_nodes")),
                    "..."
                ],
                name="conn_evaluate_knl"
            )

        @keyed_memoize_in(
            actx, (L2ProjectionInverseDiscretizationConnection,
                   "vandermonde_matrix"),
            lambda grp: grp.discretization_key()
        )
        def vandermonde_matrix(grp):
            from modepy import vandermonde
            vdm = vandermonde(grp.basis_obj().functions,
                              grp.unit_nodes)
            return actx.from_numpy(vdm)

        # evaluate at unit_nodes to get the vector on to_discr
        return DOFArray(
            actx,
            data=tuple(
                actx.call_loopy(
                    keval(),
                    vdm=vandermonde_matrix(grp),
                    coefficients=c[grp.index]
                )["result"]
                for grp in self.to_discr.groups
            )
        )


# vim: foldmethod=marker
