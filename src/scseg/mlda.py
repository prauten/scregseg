"""
=============================================================
Online Latent Dirichlet Allocation with variational inference
=============================================================
This implementation is modified from Matthew D. Hoffman's onlineldavb code
Link: https://github.com/blei-lab/onlineldavb
"""

# Author: Chyi-Kwei Yau
# Author: Matthew D. Hoffman (original onlineldavb implementation)

import numpy as np
import scipy.sparse as sp
from scipy.special import gammaln
from scipy.optimize import minimize
import warnings

from sklearn.base import BaseEstimator, TransformerMixin
from sklearn.utils import (check_random_state, check_array,
                     gen_batches, gen_even_slices)
from sklearn.utils.fixes import logsumexp
from sklearn.utils.validation import check_non_negative
from sklearn.utils._joblib import Parallel, delayed, effective_n_jobs
from sklearn.externals.six.moves import xrange
from sklearn.exceptions import NotFittedError

from sklearn.decomposition._online_lda import (mean_change, _dirichlet_expectation_1d,
                          _dirichlet_expectation_2d)

from ._hmm import _forward
from ._hmm import _backward
from ._hmm import _compute_theta_sstats
from ._hmm import _compute_beta_sstats
from ._hmm import _compute_log_reg_targets
from ._hmm import _compute_regloss_sigmoid

EPS = np.finfo(np.float).eps

def extract_seed(X, n_components, n_seeds, random_state):
    idx = random_state.permutation(X.shape[0])[:n_components*n_seeds]

    Xs = X[idx].toarray().reshape((n_seeds, n_components, X.shape[-1])).mean(axis=0) * 2
    Xs += np.ones_like(Xs)/n_components
    return Xs

def get_document_length(X):
    n_samples = X.shape[0]
    is_sparse_x = sp.issparse(X)

    dlens = np.zeros(n_samples)

    if is_sparse_x:
        X_data = X.data
        X_indices = X.indices
        X_indptr = X.indptr

    for idx_d in xrange(n_samples):
        if is_sparse_x:
            ids = X_indices[X_indptr[idx_d]:X_indptr[idx_d + 1]]
        else:
            ids = np.nonzero(X[idx_d, :])[0]
        dlens[idx_d] = len(ids)

    return dlens


def _update_doc_distribution(X, y, exp_topic_word_distr, doc_topic_prior,
                             reg_weights, max_dist,
                             max_iters,
                             mean_change_tol, cal_sstats, random_state):
    """E-step: update document-topic distribution.
    Parameters
    ----------
    X : array-like or sparse matrix, shape=(n_samples, n_features)
        Document word matrix.
    exp_topic_word_distr : dense matrix, shape=(n_topics, n_features)
        Exponential value of expectation of log topic word distribution.
        In the literature, this is `exp(E[log(beta)])`.
    doc_topic_prior : float
        Prior of document topic distribution `theta`.
    max_iters : int
        Max number of iterations for updating document topic distribution in
        the E-step.
    mean_change_tol : float
        Stopping tolerance for updating document topic distribution in E-setp.
    cal_sstats : boolean
        Parameter that indicate to calculate sufficient statistics or not.
        Set `cal_sstats` to `True` when we need to run M-step.
    random_state : RandomState instance or None
        Parameter that indicate how to initialize document topic distribution.
        Set `random_state` to None will initialize document topic distribution
        to a constant number.
    Returns
    -------
    (doc_topic_distr, suff_stats) :
        `doc_topic_distr` is unnormalized topic distribution for each document.
        In the literature, this is `gamma`. we can calculate `E[log(theta)]`
        from it.
        `suff_stats` is expected sufficient statistics for the M-step.
            When `cal_sstats == False`, this will be None.
    """
    if y is None:
        return _update_doc_distribution_lda(X,
                                            exp_topic_word_distr, doc_topic_prior,
                                            max_iters,
                                            mean_change_tol, cal_sstats, random_state)
    else:
        return _update_doc_distribution_markovlda(X, y,
                                            exp_topic_word_distr, doc_topic_prior,
                                            reg_weights, max_dist,
                                            max_iters,
                                            mean_change_tol, cal_sstats, random_state)

def _update_doc_distribution_markovlda(X, y, exp_topic_word_distr, doc_topic_prior,
                                      reg_weights, max_dist,
                                      max_iters,
                                      mean_change_tol, cal_sstats, random_state):
    """E-step: update document-topic distribution.
    Parameters
    ----------
    X : array-like or sparse matrix, shape=(n_samples, n_features)
        Document word matrix.
    exp_topic_word_distr : dense matrix, shape=(n_topics, n_features)
        Expectation of log topic word distribution.
        In the literature, this is `exp(E[log(beta)])`.
    doc_topic_prior : float
        Prior of document topic distribution `theta`.
    reg_weights :

    max_iters : int
        Max number of iterations for updating document topic distribution in
        the E-step.
    mean_change_tol : float
        Stopping tolerance for updating document topic distribution in E-setp.
    cal_sstats : boolean
        Parameter that indicate to calculate sufficient statistics or not.
        Set `cal_sstats` to `True` when we need to run M-step.
    random_state : RandomState instance or None
        Parameter that indicate how to initialize document topic distribution.
        Set `random_state` to None will initialize document topic distribution
        to a constant number.
    Returns
    -------
    (doc_topic_distr, suff_stats) :
        `doc_topic_distr` is unnormalized topic distribution for each document.
        In the literature, this is `gamma`. we can calculate `E[log(theta)]`
        from it.
        `suff_stats` is expected sufficient statistics for the M-step.
            When `cal_sstats == False`, this will be None.
    """
    is_sparse_x = sp.issparse(X)
    n_samples, n_features = X.shape
    n_topics = exp_topic_word_distr.shape[0]

    if random_state:
        doc_topic_distr = random_state.gamma(100., 0.01, (n_samples, n_topics))
    else:
        doc_topic_distr = np.ones((n_samples, n_topics))

    # In the literature, this is `E[log(theta)])`
    expected_log_doc_topic = _dirichlet_expectation_2d(doc_topic_distr)
    expected_log_topic_word_distr = exp_topic_word_distr

    # diff on `component_` (only calculate it when `cal_diff` is True)
    suff_stats = np.zeros_like(exp_topic_word_distr) if cal_sstats else None
    dist_targets = np.zeros_like(y) if cal_sstats else None

    if is_sparse_x:
        X_data = X.data
        X_indices = X.indices
        X_indptr = X.indptr

    loglikeli = 0.0

    for idx_d in xrange(n_samples):

        if is_sparse_x:
            ids = X_indices[X_indptr[idx_d]:X_indptr[idx_d + 1]]
            cnts = X_data[X_indptr[idx_d]:X_indptr[idx_d + 1]]
        else:
            ids = np.nonzero(X[idx_d, :])[0]
            cnts = X[idx_d, ids]

        # idx_d is the document index
        # ids are the word indices for a given document idx_d
        doc_topic_d = doc_topic_distr[idx_d, :]
        # The next one is a copy, since the inner loop overwrites it.
        expected_log_doc_topic_d = expected_log_doc_topic[idx_d, :].copy()
        expected_log_topic_word_d = expected_log_topic_word_distr[:, ids]

        log_sig_arg = np.zeros(len(ids) - 1)

        fwdlattice = np.zeros((len(ids), n_topics, 2))
        bwdlattice = np.zeros((len(ids), n_topics))

        log_sig_arg = y[idx_d, :(len(ids)-1)]*reg_weights[1] + reg_weights[0]
        log_sig_arg[y[idx_d, :(len(ids)-1)] > max_dist] = 100.

        # Iterate between `doc_topic_d` and `norm_phi` until convergence
        for _ in xrange(0, max_iters):
            # current theta params

            last_d = doc_topic_d

            # perform forward backward algorithm
            _backward(len(ids), n_topics, expected_log_doc_topic_d, expected_log_topic_word_d, log_sig_arg, bwdlattice)
            _forward(len(ids), n_topics, cnts, expected_log_doc_topic_d, expected_log_topic_word_d, log_sig_arg, fwdlattice, 0)

            doc_topic_d = np.zeros(n_topics)
            # collect sufficient statistcs to update doc_topic_d
            # and the regression model targets
            _compute_theta_sstats(len(ids), n_topics, cnts, fwdlattice, bwdlattice, doc_topic_d)

            # the next few lines replace the _dirichlet_expectation_1d part from before
            doc_topic_d += doc_topic_prior

            expected_log_doc_topic_d = _dirichlet_expectation_2d(doc_topic_d[None, :])
            expected_log_doc_topic_d = expected_log_doc_topic_d[0]

            if mean_change(last_d, doc_topic_d) < mean_change_tol:
                break
        doc_topic_distr[idx_d, :] = doc_topic_d

        # Contribution of document d to the expected sufficient
        # statistics for the M step.
        if cal_sstats:
            _forward(len(ids), n_topics, cnts, expected_log_doc_topic_d, expected_log_topic_word_d, log_sig_arg, fwdlattice, 0)
            _backward(len(ids), n_topics, expected_log_doc_topic_d, expected_log_topic_word_d, log_sig_arg, bwdlattice)

            log_beta_stats = np.zeros_like(suff_stats[:, ids])
            _compute_beta_sstats(len(ids), n_topics, cnts, fwdlattice, bwdlattice,
                               log_beta_stats)
            suff_stats[:, ids] += log_beta_stats

            dist_targets_d = dist_targets[idx_d, :]
            _compute_log_reg_targets(len(ids), n_topics, cnts, fwdlattice, bwdlattice,
                                 dist_targets_d)

            dist_targets[idx_d, :] = dist_targets_d

    return (doc_topic_distr, suff_stats, dist_targets)


def _update_doc_distribution_lda(X, exp_topic_word_distr, doc_topic_prior,
                             max_iters,
                             mean_change_tol, cal_sstats, random_state):
    """E-step: update document-topic distribution.
    Parameters
    ----------
    X : array-like or sparse matrix, shape=(n_samples, n_features)
        Document word matrix.
    exp_topic_word_distr : dense matrix, shape=(n_topics, n_features)
        Exponential value of expectation of log topic word distribution.
        In the literature, this is `exp(E[log(beta)])`.
    doc_topic_prior : float
        Prior of document topic distribution `theta`.
    max_iters : int
        Max number of iterations for updating document topic distribution in
        the E-step.
    mean_change_tol : float
        Stopping tolerance for updating document topic distribution in E-setp.
    cal_sstats : boolean
        Parameter that indicate to calculate sufficient statistics or not.
        Set `cal_sstats` to `True` when we need to run M-step.
    random_state : RandomState instance or None
        Parameter that indicate how to initialize document topic distribution.
        Set `random_state` to None will initialize document topic distribution
        to a constant number.
    Returns
    -------
    (doc_topic_distr, suff_stats) :
        `doc_topic_distr` is unnormalized topic distribution for each document.
        In the literature, this is `gamma`. we can calculate `E[log(theta)]`
        from it.
        `suff_stats` is expected sufficient statistics for the M-step.
            When `cal_sstats == False`, this will be None.
    """
    is_sparse_x = sp.issparse(X)
    n_samples, n_features = X.shape
    n_topics = exp_topic_word_distr.shape[0]

    if random_state:
        doc_topic_distr = random_state.gamma(100., 0.01, (n_samples, n_topics))
    else:
        doc_topic_distr = np.ones((n_samples, n_topics))

    # In the literature, this is `exp(E[log(theta)])`
    exp_doc_topic = np.exp(_dirichlet_expectation_2d(doc_topic_distr))

    # diff on `component_` (only calculate it when `cal_diff` is True)
    suff_stats = np.zeros_like(exp_topic_word_distr) if cal_sstats else None

    if is_sparse_x:
        X_data = X.data
        X_indices = X.indices
        X_indptr = X.indptr

    for idx_d in xrange(n_samples):
        if is_sparse_x:
            ids = X_indices[X_indptr[idx_d]:X_indptr[idx_d + 1]]
            cnts = X_data[X_indptr[idx_d]:X_indptr[idx_d + 1]]
        else:
            ids = np.nonzero(X[idx_d, :])[0]
            cnts = X[idx_d, ids]

        # idx_d is the document index
        # ids are the word indices for a given document idx_d
        doc_topic_d = doc_topic_distr[idx_d, :]
        # The next one is a copy, since the inner loop overwrites it.
        exp_doc_topic_d = exp_doc_topic[idx_d, :].copy()
        exp_topic_word_d = exp_topic_word_distr[:, ids]

        # Iterate between `doc_topic_d` and `norm_phi` until convergence
        for _ in xrange(0, max_iters):
            last_d = doc_topic_d

            # The optimal phi_{dwk} is proportional to
            # exp(E[log(theta_{dk})]) * exp(E[log(beta_{dw})]).
            norm_phi = np.dot(exp_doc_topic_d, exp_topic_word_d) + EPS

            #here is where we need to put the forward backward probabilities
            doc_topic_d = (exp_doc_topic_d *
                           np.dot(cnts / norm_phi, exp_topic_word_d.T))
            # Note: adds doc_topic_prior to doc_topic_d, in-place.
            _dirichlet_expectation_1d(doc_topic_d, doc_topic_prior,
                                      exp_doc_topic_d)

            if mean_change(last_d, doc_topic_d) < mean_change_tol:
                break
        doc_topic_distr[idx_d, :] = doc_topic_d

        # Contribution of document d to the expected sufficient
        # statistics for the M step.
        if cal_sstats:
            norm_phi = np.dot(exp_doc_topic_d, exp_topic_word_d) + EPS
            suff_stats[:, ids] += np.outer(exp_doc_topic_d, cnts / norm_phi) * exp_topic_word_d

    return (doc_topic_distr, suff_stats, None)


class Scseg(BaseEstimator, TransformerMixin):
    """Latent Dirichlet Allocation with online variational Bayes algorithm
    .. versionadded:: 0.17
    Read more in the :ref:`User Guide <LatentDirichletAllocation>`.
    Parameters
    ----------
    n_components : int, optional (default=10)
        Number of topics.
    doc_topic_prior : float, optional (default=None)
        Prior of document topic distribution `theta`. If the value is None,
        defaults to `1 / n_components`.
        In [1]_, this is called `alpha`.
    topic_word_prior : float, optional (default=None)
        Prior of topic word distribution `beta`. If the value is None, defaults
        to `1 / n_components`.
        In [1]_, this is called `eta`.
    learning_method : 'batch' | 'online', default='batch'
        Method used to update `_component`. Only used in `fit` method.
        In general, if the data size is large, the online update will be much
        faster than the batch update.
        Valid options::
            'batch': Batch variational Bayes method. Use all training data in
                each EM update.
                Old `components_` will be overwritten in each iteration.
            'online': Online variational Bayes method. In each EM update, use
                mini-batch of training data to update the ``components_``
                variable incrementally. The learning rate is controlled by the
                ``learning_decay`` and the ``learning_offset`` parameters.
        .. versionchanged:: 0.20
            The default learning method is now ``"batch"``.
    learning_decay : float, optional (default=0.7)
        It is a parameter that control learning rate in the online learning
        method. The value should be set between (0.5, 1.0] to guarantee
        asymptotic convergence. When the value is 0.0 and batch_size is
        ``n_samples``, the update method is same as batch learning. In the
        literature, this is called kappa.
    learning_offset : float, optional (default=10.)
        A (positive) parameter that downweights early iterations in online
        learning.  It should be greater than 1.0. In the literature, this is
        called tau_0.
    max_iter : integer, optional (default=10)
        The maximum number of iterations.
    batch_size : int, optional (default=128)
        Number of documents to use in each EM iteration. Only used in online
        learning.
    evaluate_every : int, optional (default=0)
        How often to evaluate perplexity. Only used in `fit` method.
        set it to 0 or negative number to not evalute perplexity in
        training at all. Evaluating perplexity can help you check convergence
        in training process, but it will also increase total training time.
        Evaluating perplexity in every iteration might increase training time
        up to two-fold.
    total_samples : int, optional (default=1e6)
        Total number of documents. Only used in the `partial_fit` method.
    perp_tol : float, optional (default=1e-1)
        Perplexity tolerance in batch learning. Only used when
        ``evaluate_every`` is greater than 0.
    mean_change_tol : float, optional (default=1e-3)
        Stopping tolerance for updating document topic distribution in E-step.
    max_doc_update_iter : int (default=100)
        Max number of iterations for updating document topic distribution in
        the E-step.
    n_jobs : int or None, optional (default=None)
        The number of jobs to use in the E-step.
        ``None`` means 1 unless in a :obj:`joblib.parallel_backend` context.
        ``-1`` means using all processors. See :term:`Glossary <n_jobs>`
        for more details.
    verbose : int, optional (default=0)
        Verbosity level.
    random_state : int, RandomState instance or None, optional (default=None)
        If int, random_state is the seed used by the random number generator;
        If RandomState instance, random_state is the random number generator;
        If None, the random number generator is the RandomState instance used
        by `np.random`.
    n_topics : int, optional (default=None)
        This parameter has been renamed to n_components and will
        be removed in version 0.21.
        .. deprecated:: 0.19
    Attributes
    ----------
    components_ : array, [n_components, n_features]
        Variational parameters for topic word distribution. Since the complete
        conditional for topic word distribution is a Dirichlet,
        ``components_[i, j]`` can be viewed as pseudocount that represents the
        number of times word `j` was assigned to topic `i`.
        It can also be viewed as distribution over the words for each topic
        after normalization:
        ``model.components_ / model.components_.sum(axis=1)[:, np.newaxis]``.
    n_batch_iter_ : int
        Number of iterations of the EM step.
    n_iter_ : int
        Number of passes over the dataset.

    References
    ----------
    [1] "Online Learning for Latent Dirichlet Allocation", Matthew D. Hoffman,
        David M. Blei, Francis Bach, 2010
    [2] "Stochastic Variational Inference", Matthew D. Hoffman, David M. Blei,
        Chong Wang, John Paisley, 2013
    [3] Matthew D. Hoffman's onlineldavb code. Link:
        https://github.com/blei-lab/onlineldavb

    Examples
    --------
    >>> from scseg import Scseg
    >>> from sklearn.datasets import make_multilabel_classification
    >>> # This produces a feature matrix of token counts, similar to what
    >>> # CountVectorizer would produce on text.
    >>> X, _ = make_multilabel_classification(random_state=0)
    >>> lda = Scseg(n_components=5,random_state=0)
    >>> lda.fit(X) # doctest: +ELLIPSIS
    Scseg(...)
    >>> # get topics for some given samples:
    >>> lda.transform(X[-2:])
    array([[0.00360392, 0.25499205, 0.0036211 , 0.64236448, 0.09541846],
           [0.15297572, 0.00362644, 0.44412786, 0.39568399, 0.003586  ]])
    >>> round(lda.score(X), 3)
    -14899.469
    """

    def __init__(self, n_components=10, doc_topic_prior=None,
                 topic_word_prior=None, learning_method='batch',
                 learning_decay=.7, learning_offset=10., max_iter=10,
                 batch_size=128, evaluate_every=-1, total_samples=1e6,
                 perp_tol=1e-1, mean_change_tol=1e-3, max_doc_update_iter=100,
                 n_jobs=None, verbose=0, random_state=None, n_topics=None,
                 n_seeds=None, reg_weights=None, no_regression=False, max_dist=1e7):
        self.n_components = n_components
        self.doc_topic_prior = doc_topic_prior
        self.topic_word_prior = topic_word_prior
        self.learning_method = learning_method
        self.learning_decay = learning_decay
        self.learning_offset = learning_offset
        self.max_iter = max_iter
        self.batch_size = batch_size
        self.evaluate_every = evaluate_every
        self.total_samples = total_samples
        self.perp_tol = perp_tol
        self.mean_change_tol = mean_change_tol
        self.max_doc_update_iter = max_doc_update_iter
        self.n_jobs = n_jobs
        self.verbose = verbose
        self.random_state = random_state
        self.n_topics = n_topics
        self.n_seeds = n_seeds
        self.reg_weights = reg_weights
        self.no_regression = no_regression
        self.max_dist = max_dist

    def _check_params(self):
        """Check model parameters."""
        if self.n_topics is not None:
            self._n_components = self.n_topics
            warnings.warn("n_topics has been renamed to n_components in "
                          "version 0.19 and will be removed in 0.21",
                          DeprecationWarning)
        else:
            self._n_components = self.n_components

        if self._n_components <= 0:
            raise ValueError("Invalid 'n_components' parameter: %r"
                             % self._n_components)

        if self.total_samples <= 0:
            raise ValueError("Invalid 'total_samples' parameter: %r"
                             % self.total_samples)

        if self.learning_offset < 0:
            raise ValueError("Invalid 'learning_offset' parameter: %r"
                             % self.learning_offset)

        if self.learning_method not in ("batch", "online"):
            raise ValueError("Invalid 'learning_method' parameter: %r"
                             % self.learning_method)
        self.max_dist_ = self.max_dist

    def _init_latent_vars(self, n_features, X=None):
        """Initialize latent variables."""

        self.random_state_ = check_random_state(self.random_state)
        self.n_batch_iter_ = 1
        self.n_iter_ = 0

        if self.doc_topic_prior is None:
            self.doc_topic_prior_ = 1. / self._n_components
        else:
            self.doc_topic_prior_ = self.doc_topic_prior

        if self.topic_word_prior is None:
            self.topic_word_prior_ = 1. / self._n_components
        else:
            self.topic_word_prior_ = self.topic_word_prior

        if X is None:
            init_gamma = 100.
            init_var = 1. / init_gamma
            # In the literature, this is called `lambda`
            self.components_ = self.random_state_.gamma(
                init_gamma, init_var, (self._n_components, n_features))
        else:
            seed_components = extract_seed(X, self._n_components, self.n_seeds, self.random_state_)
            self.components_ = seed_components

    def _init_log_reg_vars(self, y):
        """Initialize latent variables for regression model."""

        # define priors for regresssion model
        #if self.no_regression:
        if self.reg_weights is None:
            # this behaves exactly as the LDA
            self.reg_weights_ = np.asarray([-1., 1.])
        else:
            self.reg_weights_ = self.reg_weights



    def _e_step(self, X, y, cal_sstats, random_init, parallel=None):
        """E-step in EM update.
        Parameters
        ----------
        X : array-like or sparse matrix, shape=(n_samples, n_features)
            Document word matrix.
        cal_sstats : boolean
            Parameter that indicate whether to calculate sufficient statistics
            or not. Set ``cal_sstats`` to True when we need to run M-step.
        random_init : boolean
            Parameter that indicate whether to initialize document topic
            distribution randomly in the E-step. Set it to True in training
            steps.
        parallel : joblib.Parallel (optional)
            Pre-initialized instance of joblib.Parallel.
        Returns
        -------
        (doc_topic_distr, suff_stats) :
            `doc_topic_distr` is unnormalized topic distribution for each
            document. In the literature, this is called `gamma`.
            `suff_stats` is expected sufficient statistics for the M-step.
            When `cal_sstats == False`, it will be None.
        """

        # Run e-step in parallel
        random_state = self.random_state_ if random_init else None

        # TODO: make Parallel._effective_n_jobs public instead?
        n_jobs = effective_n_jobs(self.n_jobs)
        if parallel is None:
            parallel = Parallel(n_jobs=n_jobs, verbose=max(0,
                                self.verbose - 1))

        results = parallel(
            delayed(_update_doc_distribution)(X[idx_slice, :],
                                              y[idx_slice, :] if y is not None else None,
                                              self.exp_dirichlet_component_,
                                              self.doc_topic_prior_,
                                              self.reg_weights_ if hasattr(self, "reg_weights_") else None,
                                              self.max_dist_,
                                              self.max_doc_update_iter,
                                              self.mean_change_tol, cal_sstats,
                                              random_state)
            for idx_slice in gen_even_slices(X.shape[0], n_jobs))

        # merge result
        doc_topics, sstats_list, reg_targets = zip(*results)
        doc_topic_distr = np.vstack(doc_topics)

        reg_targets_all = None
        suff_stats = None
        if cal_sstats:
            # This step finishes computing the sufficient statistics for the
            # M-step.
            suff_stats = np.zeros_like(self.components_)
            for sstats in sstats_list:
                suff_stats += sstats

            reg_targets_all = np.vstack(reg_targets)

        return (doc_topic_distr, suff_stats, reg_targets_all)

    def _em_step(self, X, y, l, total_samples, batch_update, parallel=None):
        """EM update for 1 iteration.
        update `_component` by batch VB or online VB.
        Parameters
        ----------
        X : array-like or sparse matrix, shape=(n_samples, n_features)
            Document word matrix.
        y : None or array-like
            Between word distances within each document
        l : None or array-like
            Number of distances within each document
        total_samples : integer
            Total number of documents. It is only used when
            batch_update is `False`.
        batch_update : boolean
            Parameter that controls updating method.
            `True` for batch learning, `False` for online learning.
        parallel : joblib.Parallel
            Pre-initialized instance of joblib.Parallel
        Returns
        -------
        doc_topic_distr : array, shape=(n_samples, n_components)
            Unnormalized document topic distribution.
        """

        # E-step
        _, suff_stats, reg_targets = self._e_step(X, y, cal_sstats=True, random_init=True,
                                     parallel=parallel)

        # M-step
        if batch_update:
            self.components_ = self.topic_word_prior_ + suff_stats
        else:
            # online update
            # In the literature, the weight is `rho`
            weight = np.power(self.learning_offset + self.n_batch_iter_,
                              -self.learning_decay)
            doc_ratio = float(total_samples) / X.shape[0]
            self.components_ *= (1 - weight)
            self.components_ += (weight * (self.topic_word_prior_
                                           + doc_ratio * suff_stats))

        if hasattr(self, "reg_weights_") and y is not None and not self.no_regression:

            dists = y

            def objective(weights, lens_, dists_, reg_targets_, max_dist_):
                return _compute_regloss_sigmoid(lens_.astype('int32'), weights, dists_, reg_targets_, max_dist_)

            ores = minimize(objective, self.reg_weights_, args=(l, dists, reg_targets, self.max_dist_))
            self.reg_weights_ = ores.x


        self.exp_dirichlet_component_ = _dirichlet_expectation_2d(self.components_)
        if y is None:
            self.exp_dirichlet_component_ = np.exp(self.exp_dirichlet_component_)
        # update `component_` related variables
#        self.exp_dirichlet_component_ = np.exp(
#            _dirichlet_expectation_2d(self.components_))
        self.n_batch_iter_ += 1
        return

    def _check_non_neg_array(self, X, whom):
        """check X format
        check X format and make sure no negative value in X.
        Parameters
        ----------
        X :  array-like or sparse matrix
        """
        X = check_array(X, accept_sparse='csr')
        check_non_negative(X, whom)
        return X

    def fit(self, X, y=None):
        """Learn model for the data X with variational Bayes method.
        When `learning_method` is 'online', use mini-batch update.
        Otherwise, use batch update.
        Parameters
        ----------
        X : array-like or sparse matrix, shape=(n_samples, n_features)
            Document word matrix.
        y : None or array-like, shape=(n_samples, n_max_words)
            If None, a normal LDA will be fitted.
            Otherwise, y represents the distances between words for each
            document.

        Returns
        -------
        self
        """
        self._check_params()
        X = self._check_non_neg_array(X, "LatentDirichletAllocation.fit")
        n_samples, n_features = X.shape
        max_iter = self.max_iter
        evaluate_every = self.evaluate_every
        learning_method = self.learning_method

        batch_size = self.batch_size

        # initialize parameters
        if self.n_seeds is None:
            self._init_latent_vars(n_features)
        else:
            self._init_latent_vars(n_features, X)

        self.exp_dirichlet_component_ = _dirichlet_expectation_2d(self.components_)
        if y is None:
            # In the literature, this is `exp(E[log(beta)])`
            self.exp_dirichlet_component_ = np.exp(self.exp_dirichlet_component_)

        dlens = None
        if y is not None:
            self._init_log_reg_vars(y)
            dlens = get_document_length(X)

        # change to perplexity later
        last_bound = None
        n_jobs = effective_n_jobs(self.n_jobs)
        with Parallel(n_jobs=n_jobs, verbose=max(0,
                      self.verbose - 1)) as parallel:
            for i in xrange(max_iter):
                if learning_method == 'online':
                    for idx_slice in gen_batches(n_samples, batch_size):
                        self._em_step(X[idx_slice, :],
                                      y[idx_slice] if y is not None else None,
                                      dlens[idx_slice] if dlens is not None else None,
                                      total_samples=n_samples,
                                      batch_update=False, parallel=parallel)
                else:
                    # batch update
                    self._em_step(X, y, dlens, total_samples=n_samples,
                                  batch_update=True, parallel=parallel)

                # check perplexity
                if evaluate_every > 0 and (i + 1) % evaluate_every == 0:
                    doc_topics_distr, _, _ = self._e_step(X, y,
                                                       cal_sstats=False,
                                                       random_init=False,
                                                       parallel=parallel)
                    bound = self._perplexity_precomp_distr(X, y, doc_topics_distr,
                                                           sub_sampling=False)
                    if self.verbose:
                        print('iteration: %d of max_iter: %d, perplexity: %.4f'
                              % (i + 1, max_iter, bound))

                    if last_bound and abs(last_bound - bound) < self.perp_tol:
                        break
                    last_bound = bound

                elif self.verbose:
                    print('iteration: %d of max_iter: %d' % (i + 1, max_iter))
                self.n_iter_ += 1

        # calculate final perplexity value on train set
        doc_topics_distr, _, _ = self._e_step(X, y, cal_sstats=False,
                                           random_init=False,
                                           parallel=parallel)
        self.bound_ = self._perplexity_precomp_distr(X, y, doc_topics_distr,
                                                     sub_sampling=False)

        return self

    def _unnormalized_transform(self, X, y):
        """Transform data X according to fitted model.
        Parameters
        ----------
        X : array-like or sparse matrix, shape=(n_samples, n_features)
            Document word matrix.
        Returns
        -------
        doc_topic_distr : shape=(n_samples, n_components)
            Document topic distribution for X.
        """
        if not hasattr(self, 'components_'):
            raise NotFittedError("no 'components_' attribute in model."
                                 " Please fit model first.")

        # make sure feature size is the same in fitted model and in X
        X = self._check_non_neg_array(X, "LatentDirichletAllocation.transform")
        n_samples, n_features = X.shape
        if n_features != self.components_.shape[1]:
            raise ValueError(
                "The provided data has %d dimensions while "
                "the model was trained with feature size %d." %
                (n_features, self.components_.shape[1]))

        doc_topic_distr, _, _ = self._e_step(X, y, cal_sstats=False,
                                          random_init=False)

        return doc_topic_distr

    def transform(self, X, y=None):
        """Transform data X according to the fitted model.
           .. versionchanged:: 0.18
              *doc_topic_distr* is now normalized
        Parameters
        ----------
        X : array-like or sparse matrix, shape=(n_samples, n_features)
            Document word matrix.
        Returns
        -------
        doc_topic_distr : shape=(n_samples, n_components)
            Document topic distribution for X.
        """
        doc_topic_distr = self._unnormalized_transform(X, y)
        doc_topic_distr /= doc_topic_distr.sum(axis=1)[:, np.newaxis]
        return doc_topic_distr

    def compute_likelihood(self, X, y, doc_topic_distr):

        is_sparse_x = sp.issparse(X)
        n_samples, n_components = doc_topic_distr.shape
        n_features = self.components_.shape[1]
        score = 0

        dirichlet_doc_topic = _dirichlet_expectation_2d(doc_topic_distr)
        dirichlet_component_ = _dirichlet_expectation_2d(self.components_)

        if is_sparse_x:
            X_data = X.data
            X_indices = X.indices
            X_indptr = X.indptr

        # E[log p(docs | theta, beta)]
        for idx_d in xrange(0, n_samples):
            if is_sparse_x:
                ids = X_indices[X_indptr[idx_d]:X_indptr[idx_d + 1]]
                cnts = X_data[X_indptr[idx_d]:X_indptr[idx_d + 1]]
            else:
                ids = np.nonzero(X[idx_d, :])[0]
                cnts = X[idx_d, ids]
            expected_log_doc_topic_d = dirichlet_doc_topic[idx_d]
            expected_log_topic_word_d = dirichlet_component_[:, ids]
            if y is None:

                # Likeihood for LDA
                temp = (expected_log_doc_topic_d[:, np.newaxis] + expected_log_topic_word_d)
                norm_phi = logsumexp(temp, axis=0)
                score += np.dot(cnts, norm_phi)
            else:
                # likelihood for the markov lda

                log_sig_arg = y[idx_d, :(len(ids)-1)]*self.reg_weights_[1] + self.reg_weights_[0]
                fwdlattice = np.zeros((len(ids), self._n_components, 2))
                score += _forward(len(ids), self._n_components, cnts, expected_log_doc_topic_d,
                         expected_log_topic_word_d, log_sig_arg, fwdlattice, 1)
        return score


    def _approx_bound(self, X, y, doc_topic_distr, sub_sampling):
        """Estimate the variational bound.
        Estimate the variational bound over "all documents" using only the
        documents passed in as X. Since log-likelihood of each word cannot
        be computed directly, we use this bound to estimate it.
        Parameters
        ----------
        X : array-like or sparse matrix, shape=(n_samples, n_features)
            Document word matrix.
        doc_topic_distr : array, shape=(n_samples, n_components)
            Document topic distribution. In the literature, this is called
            gamma.
        sub_sampling : boolean, optional, (default=False)
            Compensate for subsampling of documents.
            It is used in calculate bound in online learning.
        Returns
        -------
        score : float
        """
        n_samples, n_components = doc_topic_distr.shape
        n_features = self.components_.shape[1]
        dirichlet_doc_topic = _dirichlet_expectation_2d(doc_topic_distr)
        dirichlet_component_ = _dirichlet_expectation_2d(self.components_)
        doc_topic_prior = self.doc_topic_prior_
        topic_word_prior = self.topic_word_prior_

        def _loglikelihood(prior, distr, dirichlet_distr, size):
            # calculate log-likelihood
            score = np.sum((prior - distr) * dirichlet_distr)
            score += np.sum(gammaln(distr) - gammaln(prior))
            score += np.sum(gammaln(prior * size) - gammaln(np.sum(distr, 1)))
            return score

        score = self.compute_likelihood(X, y, doc_topic_distr)

        # compute E[log p(theta | alpha) - log q(theta | gamma)]
        score_theta = _loglikelihood(doc_topic_prior, doc_topic_distr,
                                dirichlet_doc_topic, self._n_components)

        score += score_theta
        # Compensate for the subsampling of the population of documents
        if sub_sampling:
            doc_ratio = float(self.total_samples) / n_samples
            score *= doc_ratio

        # E[log p(beta | eta) - log q (beta | lambda)]
        score_beta = _loglikelihood(topic_word_prior, self.components_,
                                dirichlet_component_, n_features)

        score += score_beta
        return score

    def score(self, X, y=None):
        """Calculate approximate log-likelihood as score.
        Parameters
        ----------
        X : array-like or sparse matrix, shape=(n_samples, n_features)
            Document word matrix.
        y : Ignored
        Returns
        -------
        score : float
            Use approximate bound as score.
        """
        X = self._check_non_neg_array(X, "LatentDirichletAllocation.score")

        doc_topic_distr = self._unnormalized_transform(X, y)
        score = self._approx_bound(X, y, doc_topic_distr, sub_sampling=False)
        return score

    def _perplexity_precomp_distr(self, X, y, doc_topic_distr=None,
                                  sub_sampling=False):
        """Calculate approximate perplexity for data X with ability to accept
        precomputed doc_topic_distr
        Perplexity is defined as exp(-1. * log-likelihood per word)
        Parameters
        ----------
        X : array-like or sparse matrix, [n_samples, n_features]
            Document word matrix.
        doc_topic_distr : None or array, shape=(n_samples, n_components)
            Document topic distribution.
            If it is None, it will be generated by applying transform on X.
        Returns
        -------
        score : float
            Perplexity score.
        """
        if not hasattr(self, 'components_'):
            raise NotFittedError("no 'components_' attribute in model."
                                 " Please fit model first.")

        X = self._check_non_neg_array(X,
                                      "LatentDirichletAllocation.perplexity")

        if doc_topic_distr is None:
            doc_topic_distr = self._unnormalized_transform(X, y)
        else:
            n_samples, n_components = doc_topic_distr.shape
            if n_samples != X.shape[0]:
                raise ValueError("Number of samples in X and doc_topic_distr"
                                 " do not match.")

            if n_components != self._n_components:
                raise ValueError("Number of topics does not match.")

        current_samples = X.shape[0]
        bound = self._approx_bound(X, y, doc_topic_distr, sub_sampling)

        if sub_sampling:
            word_cnt = X.sum() * (float(self.total_samples) / current_samples)
        else:
            word_cnt = X.sum()
        perword_bound = bound / word_cnt

        return np.exp(-1.0 * perword_bound)

    def perplexity(self, X, y=None, doc_topic_distr='deprecated', sub_sampling=False):
        """Calculate approximate perplexity for data X.
        Perplexity is defined as exp(-1. * log-likelihood per word)
        .. versionchanged:: 0.19
           *doc_topic_distr* argument has been deprecated and is ignored
           because user no longer has access to unnormalized distribution
        Parameters
        ----------
        X : array-like or sparse matrix, [n_samples, n_features]
            Document word matrix.
        doc_topic_distr : None or array, shape=(n_samples, n_components)
            Document topic distribution.
            This argument is deprecated and is currently being ignored.
            .. deprecated:: 0.19
        sub_sampling : bool
            Do sub-sampling or not.
        Returns
        -------
        score : float
            Perplexity score.
        """
        if doc_topic_distr != 'deprecated':
            warnings.warn("Argument 'doc_topic_distr' is deprecated and is "
                          "being ignored as of 0.19. Support for this "
                          "argument will be removed in 0.21.",
                          DeprecationWarning)

        return self._perplexity_precomp_distr(X, y, sub_sampling=sub_sampling)
