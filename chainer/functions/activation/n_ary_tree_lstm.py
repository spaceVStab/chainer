import numpy
import six

from chainer import cuda
from chainer import function
from chainer.utils import type_check


def _extract_gates(x, n_split=5):
    """Extract gates by split.

    This is a different from ``_extract_gates`` in lstm.py,
    which is as follows
    ```
        r = x.reshape((x.shape[0], x.shape[1] // 4, 4) + x.shape[2:])
        return (r[:, :, i] for i in six.moves.range(4))
    ```
    In other words, it thinly slices x and merge them,
    while this thickly slices x.

    """
    r = x.reshape(
        (x.shape[0], n_split, x.shape[1] // n_split) + x.shape[2:])
    return (r[:, i, :] for i in six.moves.range(n_split))


def _sigmoid(x):
    half = x.dtype.type(0.5)
    return numpy.tanh(x * half) * half + half


def _grad_sigmoid(x):
    return x * (1 - x)


def _grad_tanh(x):
    return 1 - x * x


_preamble = '''
template <typename T> __device__ T sigmoid(T x) {
    const T half = 0.5;
    return tanh(x * half) * half + half;
}
template <typename T> __device__ T grad_sigmoid(T y) { return y * (1 - y); }
template <typename T> __device__ T grad_tanh(T y) { return 1 - y * y; }

#define COMMON_ROUTINE \
    T aa = tanh(a); \
    T ai = sigmoid(i_); \
    T ao = sigmoid(o); \
'''


class NaryTreeLSTM(function.Function):

    """N-ary TreeLSTM unit with two forget gates.

    Modified from Tai et al. (arxiv:1503.00075) and exactly as in Bowman et al.
    (arxiv:1603.06021); we have variable inputs (c1, c2, ..., cN, x)
    where x is (3 + N) times larger in the feature dimension and represents
    everything inside the activation functions. This means the modified version
    has an additional independent matrix to Tai; in particular,
    f1, f2, ..., fN can depend in different ways on
    the TreeLSTM input from the current node. There are two outputs (c, h).

    """

    def check_type_forward(self, in_types):
        type_check.expect(in_types.size() >= 3)
        c_types = in_types[:-1]
        x_type = in_types[-1]
        n_ary = len(c_types)

        type_check.expect(x_type.ndim >= 2)
        for i in six.moves.range(len(c_types)):
            type_check.expect(
                c_types[i].dtype.kind == 'f',
                x_type.dtype == c_types[i].dtype,
                c_types[i].ndim >= 2,
                c_types[i].ndim == x_type.ndim,
                x_type.shape[0] == c_types[i].shape[0],
                x_type.shape[1] == (3 + n_ary) * c_types[i].shape[1],
            )
            for j in six.moves.range(2, type_check.eval(c_types[i].ndim)):
                type_check.expect(x_type.shape[i] == c_types[i].shape[j])

    def forward(self, inputs):
        cs, x = inputs[:-1], inputs[-1]
        n_ary = len(cs)
        gates = list(_extract_gates(x, 3 + n_ary))
        a, i, o = gates[:3]
        fs = gates[3:]

        if isinstance(x, numpy.ndarray):
            self.a = numpy.tanh(a)
            self.i = _sigmoid(i)
            self.o = _sigmoid(o)
            self.fs = [_sigmoid(f) for f in fs]

            self.c = self.a * self.i + sum(f * c for f, c in zip(self.fs, cs))
            h = self.o * numpy.tanh(self.c)
        else:
            preamble = _preamble + \
                ' '.join('T af{} = sigmoid(f{});'.format(j, j)
                         for j in six.moves.range(1, n_ary + 1))
            cells_str = ', '.join('T c{}'.format(j)
                                  for j in six.moves.range(1, n_ary + 1))
            fgates_str = ', '.join('T f{}'.format(j)
                                   for j in six.moves.range(1, n_ary + 1))
            fc_calc_str = ' + '.join('af{} * c{}'.format(j, j)
                                     for j in six.moves.range(1, n_ary + 1))
            self.c, h = cuda.elementwise(
                'T a, T i_, T o, {}, {}'.format(cells_str, fgates_str),
                'T c, T h',
                '''
                    COMMON_ROUTINE;
                    c = aa * ai + {};
                    h = ao * tanh(c);
                '''.format(fc_calc_str),
                'treelstm_fwd', preamble=preamble)(
                    a, i, o, *(list(cs) + fs))

        return self.c, h

    def backward(self, inputs, grad_outputs):
        xp = cuda.get_array_module(*inputs)
        cs, x = inputs[:-1], inputs[-1]
        n_ary = len(cs)
        gc, gh = grad_outputs

        gx = xp.empty_like(x)
        gates = list(_extract_gates(gx, 3 + n_ary))
        ga, gi, go = gates[:3]
        gfs = gates[3:]

        # Consider the case that either gradient is not given
        if gc is None:
            gc = 0
        if gh is None:
            gh = 0

        if xp is numpy:
            co = numpy.tanh(self.c)
            tmp = gh * self.o * _grad_tanh(co) + gc
            ga[:] = tmp * self.i * _grad_tanh(self.a)
            gi[:] = tmp * self.a * _grad_sigmoid(self.i)
            go[:] = gh * co * _grad_sigmoid(self.o)

            gcs = []
            for j in six.moves.range(0, n_ary):
                gfs[j][:] = tmp * cs[j] * _grad_sigmoid(self.fs[j])
                gcs.append(tmp * self.fs[j])
        else:
            gates = list(_extract_gates(x, 3 + n_ary))
            a, i, o = gates[:3]
            fs = gates[3:]
            gcs = [xp.empty_like(c) for c in cs]
            preamble = _preamble + \
                ' '.join('T af{} = sigmoid(f{});'.format(j, j)
                         for j in six.moves.range(1, n_ary + 1))
            cells_str = ', '.join('T c{}'.format(j)
                                  for j in six.moves.range(1, n_ary + 1))
            fgates_str = ', '.join('T f{}'.format(j)
                                   for j in six.moves.range(1, n_ary + 1))
            g_cells_str = ', '.join('T gc{}'.format(j)
                                    for j in six.moves.range(1, n_ary + 1))
            g_fgates_str = ', '.join('T gf{}'.format(j)
                                     for j in six.moves.range(1, n_ary + 1))
            gf_calc_str = '\n    '.join(
                'gf{} = temp * c{} * grad_sigmoid(af{});'.format(j, j, j)
                for j in six.moves.range(1, n_ary + 1))
            gc_calc_str = '\n    '.join(
                'gc{} = temp * af{};'.format(j, j)
                for j in six.moves.range(1, n_ary + 1))
            cuda.elementwise(
                'T c, T gc, T gh, T a, T i_, T o, ' +
                '{}, {}'.format(cells_str, fgates_str),
                'T ga, T gi, T go, {}, {}'.format(g_cells_str, g_fgates_str),
                '''
                    COMMON_ROUTINE;
                    T co = tanh(c);
                    T temp = gh * ao * grad_tanh(co) + gc;
                    ga = temp * ai * grad_tanh(aa);
                    gi = temp * aa * grad_sigmoid(ai);
                    go = gh * co * grad_sigmoid(ao);
                    {}
                    {}
                '''.format(gf_calc_str, gc_calc_str),
                'treelstm_bwd', preamble=preamble)(
                    self.c, gc, gh, a, i, o,
                    *(list(cs) + fs + [ga, gi, go] + gcs + gfs))

        return list(gcs) + [gx]


def n_ary_tree_lstm(*inputs):
    """N-ary TreeLSTM unit as an activation function.

    This function implements N-ary TreeLSTM units, which is proposed
    by Tai et al. and modified by Bowman et al. Let the
    previous cell states :math:`c_{\\text{1}}` `:math:c_{\\text{2}}`
    ... `:math:c_{\\text{N}}`,
    and the incoming signal :math:`x`.

    First, the incoming signal :math:`x` is split into (3 + N) arrays
    :math:`a, i, o, f1, f2, ..., fN` of the same shapes along the second axis.
    It means that :math:`x` 's second axis must have (3 + N) times
    of the length of each :math:`c_{\\text{n}}`.

    The splitted input signals are corresponding to:

        - :math:`a` : sources of cell input
        - :math:`i` : sources of input gate
        - :math:`o` : sources of output gate
        - :math:`fn` : sources of forget gate for n-th ary

    Second, it computes outputs as:

    .. math::

        c &= \\tanh(a) \\text{sigmoid}(i) \\\\
           + c_{\\text{1}} \\text{sigmoid}(f1), \\\\
           + c_{\\text{2}} \\text{sigmoid}(f2), \\\\
           + ..., \\\\
           + c_{\\text{N}} \\text{sigmoid}(fN), \\\\
        h &= \\tanh(c) \\text{sigmoid}(o).

    These are returned as a tuple of (N + 1) variables.

    Args:
        inputs (list of ~chainer.Variable): Variable arguments which include
            all cell vectors from child-nodes, and an input vector.
            Each of the cell vectors and the input vector is ~chainer.Variable.
            The input vector must have the second dimension whose size
            is (N + 3) times of that of each cell,
            where N denotes the total number of cells.

    Returns:
        tuple: Two :class:`~chainer.Variable` objects ``c`` and ``h``. ``c`` is
            the updated cell state. ``h`` indicates the outgoing signal.

    See Tai et al. paper's proposal for N-Ary Tree-LSTM (Sec. 3.2, but note
        that Eq. 10 only has one W matrix, applied to x, for all children,
        while we have one for each, as shown in Bowman et al. paper):
    `Improved Semantic Representations From Tree-Structured Long \
    Short-Term Memory Networks \
    <http://arxiv.org/pdf/1503.00075v3.pdf>`_.
    `A Fast Unified Model for Parsing and Sentence Understanding \
    <https://arxiv.org/pdf/1603.06021.pdf>`_.

    .. admonition:: Example

        Assuming ``y`` is the current input signal, ``c`` is the previous cell
        state, and ``h`` is the previous output signal from an
        ``n_ary_tree_lstm`` function.
        Each of ``y``, ``c`` and ``h`` has ``n_units`` channels.
        Using 2-ary (binary) TreeLSTM,
        most typical preparation of ``x`` is:

        >>> model = FunctionSet(w=F.Linear(n_units, 5 * n_units),
        ...                     v1=F.Linear(n_units, 5 * n_units),
        ...                     v2=F.Linear(n_units, 5 * n_units),
        ...                     ...)
        >>> x = model.w(y) + model.v1(h1) + model.v2(h2)
        >>> c, h = F.n_ary_tree_lstm(c1, c2, x)

        It corresponds to calculate the input sources :math:`a, i, o, f1, f2`
        from the current input ``y`` and the children's outputs
        ``h1`` and ``h2``. Different parameters are used for different kind of
        input sources.

    """
    return NaryTreeLSTM()(*inputs)
