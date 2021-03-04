"""

"""
# License: BSD 3 clause


import numbers
from warnings import catch_warnings, simplefilter, warn
import threading

from abc import ABCMeta, abstractmethod
import numpy as np

# from scipy.sparse import issparse
# from scipy.sparse import hstack as sparse_hstack
from joblib import Parallel

from time import time

from sklearn.ensemble._base import _partition_estimators

from sklearn.base import ClassifierMixin, RegressorMixin, MultiOutputMixin

from sklearn.utils import check_random_state, check_array, compute_sample_weight

from sklearn.base import BaseEstimator, ClassifierMixin
from sklearn.utils.validation import (
    check_is_fitted,
    check_consistent_length,
    _check_sample_weight,
)
from sklearn.preprocessing import LabelEncoder


# from ..exceptions import DataConversionWarning
# from ._base import BaseEnsemble, _partition_estimators
from sklearn.utils.fixes import _joblib_parallel_args, delayed
from sklearn.utils.multiclass import check_classification_targets
from sklearn.utils.validation import check_is_fitted, _check_sample_weight

from ._binning import Binner

# from ._utils import np_size_t, max_size_t

__all__ = ["ForestBinaryClassifier"]

# from wildwood._utils import MAX_INT

from wildwood.tree import TreeBinaryClassifier


def _generate_train_valid_samples(random_state, n_samples, tree_idx):
    """
    This functions generates "in-the-bag" (train) and "out-of-the-bag" samples

    Parameters
    ----------
    random_state : None or int or RandomState
        Allows to specify the RandomState used for random splitting

    n_samples : int
        Total number of samples

    Returns
    -------
    output : tuple of theer numpy arrays
        * output[0] contains the indices of the training samples
        * output[1] contains the indices of the validation samples
        * output[2] contains the counts of the training samples
    """
    # TODO: add the tree_idx to get different indices ???
    random_instance = check_random_state(random_state + tree_idx)
    # Sample the bootstrap samples (uniform sampling with replacement)

    sample_indices = random_instance.randint(0, n_samples, n_samples)
    sample_counts = np.bincount(sample_indices, minlength=n_samples)
    indices = np.arange(n_samples)
    valid_mask = sample_counts == 0
    # For very small samples, we might end up with empty validation...
    if valid_mask.sum() == 0:
        return _generate_train_valid_samples((random_state + 1) % np.iinfo(
            np.uintp).max, n_samples)
    else:
        # print("sample_indices: ", sample_indices)
        # print("sample_counts: ", sample_counts)
        train_mask = np.logical_not(valid_mask)
        train_indices = indices[train_mask].astype(np.uintp)
        valid_indices = indices[valid_mask].astype(np.uintp)
        train_indices_count = sample_counts[train_mask].astype(np.uintp)
        return train_indices, valid_indices, train_indices_count


# TODO: faudrait que la parallelisation marche des de le debut...
def _parallel_build_trees(tree, X, y, sample_weight, tree_idx, n_trees, verbose=0):
    """
    Private function used to fit a single tree in parallel.
    """
    # if verbose > 1:
    print("building tree %d of %d" % (tree_idx + 1, n_trees))

    n_samples = X.shape[0]
    if sample_weight is None:
        sample_weight = np.ones((n_samples,), dtype=np.float32)
    else:
        # Do a copy with float32 dtype
        sample_weight = sample_weight.astype(np.float32)

    # TODO: all trees have the same train and valid indices...
    train_indices, valid_indices, train_indices_count = _generate_train_valid_samples(
        tree.random_state, n_samples, tree_idx
    )

    # print("train_indices:", train_indices)
    # print("valid_indices:", valid_indices)

    # sample_weight_train = sample_weight_full[train_indices]
    # sample_weight_valid = sample_weight_full[valid_indices]
    # We use bootstrap: sample repetition is achieved by multiplying the sample
    # weights by the sample counts. By construction, no repetition is possible in
    # validation data
    sample_weight[train_indices] *= train_indices_count

    # TODO: je ne comprends pas les lignes commentees suivantes. Pour l'instant,
    #  il faut que sample weight contienne deja les poids si
    #  class_weighted="balanced" dans la foret. NB : je crois que ca vient du fait
    #  qu'on peut appeler plusieurs fois fit avec des sample_weight differents
    # if class_weight == "subsample":
    #     with catch_warnings():
    #         simplefilter("ignore", DeprecationWarning)
    #         curr_sample_weight *= compute_sample_weight("auto", y, indices=indices)
    # elif class_weight == "balanced_subsample":
    #     curr_sample_weight *= compute_sample_weight("balanced", y, indices=indices)

    tic = time()
    tree.fit(
        X,
        y,
        train_indices,
        valid_indices,
        sample_weight,
        check_input=False,
    )
    toc = time()
    print("Done building tree %d with %d nodes in %.2f s" % (tree_idx,
                                                              tree.n_nodes_, toc-tic))

    return tree


def _accumulate_prediction(predict, X, out, lock):
    """
    This is a utility function for joblib's Parallel.

    It can't go locally in ForestClassifier or ForestRegressor, because joblib
    complains that it cannot pickle it when placed there.
    """
    prediction = predict(X, check_input=False)
    with lock:
        out += prediction
        # if len(out) == 1:
        #     out[0] += prediction
        # else:
        #     for i in range(len(out)):
        #         out[i] += prediction[i]


def _get_tree_prediction(predict, X, out, lock, tree_idx):
    prediction = predict(X, check_input=False)
    with lock:
        out[tree_idx] = prediction


# class ForestClassifier(ClassifierMixin, BaseForest, metaclass=ABCMeta):
#     """
#     Base class for forest of trees-based classifiers.
#
#     Warning: This class should not be used directly. Use derived classes
#     instead.
#     """
#
#     @abstractmethod
#     def __init__(
#         self,
#         base_estimator,
#         n_estimators=100,
#         *,
#         estimator_params=tuple(),
#         bootstrap=False,
#         oob_score=False,
#         n_jobs=None,
#         random_state=None,
#         verbose=0,
#         warm_start=False,
#         class_weight=None,
#         max_samples=None
#     ):
#         super().__init__(
#             base_estimator,
#             n_estimators=n_estimators,
#             estimator_params=estimator_params,
#             bootstrap=bootstrap,
#             oob_score=oob_score,
#             n_jobs=n_jobs,
#             random_state=random_state,
#             verbose=verbose,
#             warm_start=warm_start,
#             class_weight=class_weight,
#             max_samples=max_samples,
#         )


class ForestBinaryClassifier(BaseEstimator, ClassifierMixin):
    """
    WildWood for binary classification.

    It grows in parallel `n_estimator` trees using bootstrap samples and aggregates
    their predictions (bagging). Each tree uses "in-the-bag" samples to grow itself
    and "out-of-the-bag" samples to compute aggregation weights for all possible
    subtrees of the whole tree.

    The prediction function of each tree in WildWood is very different from the one
    of a standard decision trees. Indeed, the predictions of a tree are computed here
    as an aggregation with exponential weights of all the predictions given by all
    possible subtrees (prunings) of the full tree. The required computations are
    performed efficiently thanks to a variant of the context tree weighting algorithm.

    Also, features are all binned with a maximum of 255 bins, following lightGBM's
    binning strategy.


    TODO: update doc link
    Read more in the :ref:`User Guide <forest>`.

    Parameters
    ----------
    n_estimators : int, default=100
        The number of trees in the forest.

    criterion : {"gini", "entropy"}, default="gini"
        The impurity criterion used to measure the quality of a split. The supported
        impurity criteria are "gini" for the Gini impurity and "entropy" for the
        entropy impurity.

    loss : {"log"}, default="log"
        The loss used for the computation of the aggregation weights. Only "log"
        is supported for now, namely the log-loss for classification.

    step : float, default=1
        Step-size for the aggregation weights. Default is 1 for classification with
        the log-loss, which is usually the best choice. A larger value will lead to
        aggregation weights with the best validation loss.

    aggregation : bool, default=True
        Controls if aggregation is used in the trees. It is highly recommended to
        leave it as `True`.

    dirichlet : float, default=0.5
        Regularization level of the class frequencies used for predictions in each
        node. A good default is dirichlet=0.5 for binary classification.

    max_depth : int, default=None
        The maximum depth of a tree. If None, then nodes from the tree are split until
        they are "pure" (impurity is small enough) or until they contain
        min_samples_split samples.

    min_samples_split : int or float, default=2
        The minimum number of samples required to split an internal node (not a leaf)

    min_samples_leaf : int or float, default=1
        The minimum number of samples required to be in a leaf node.
        A split point at any depth will only be considered if it leaves at
        least ``min_samples_leaf`` samples in the left and right childs.

    max_bins : int, default=255
        The maximum number of bins to use for non-missing values. Before
        training, each feature of the input array `X` is binned into
        integer-valued bins, which allows for a much faster training stage.
        Features with a small number of unique values may use less than
        ``max_bins`` bins. In addition to the ``max_bins`` bins, one more bin
        is always reserved for missing values. Must be no larger than 255.

    categorical_features : array-like of {bool, int} of shape (n_features) \
            or shape (n_categorical_features,), default=None.
        Indicates the categorical features.

        - None : no feature will be considered categorical.
        - boolean array-like : boolean mask indicating categorical features.
        - integer array-like : integer indices indicating categorical
          features.

        For each categorical feature, there must be at most `max_bins` unique
        categories, and each categorical value must be in [0, max_bins -1].

        Read more in the :ref:`User Guide <categorical_support_gbdt>`.

        .. versionadded:: 0.24

    max_features : {"auto", "sqrt", "log2"}, int or float, default="auto"
        The number of features to consider when looking for the best split:

        - If int, then consider `max_features` features at each split.
        - If float, then `max_features` is a fraction and
          `round(max_features * n_features)` features are considered at each
          split.
        - If "auto", then `max_features=sqrt(n_features)`.
        - If "sqrt", then `max_features=sqrt(n_features)` (same as "auto").
        - If "log2", then `max_features=log2(n_features)`.
        - If None, then `max_features=n_features`.

        Note: the search for a split does not stop until at least one
        valid partition of the node samples is found, even if it requires to
        effectively inspect more than ``max_features`` features.
        TODO: this is not true for now...

    n_jobs : int, default=1
        The number of jobs to run in parallel for :meth:`fit`, :meth:`predict`,
        :meth:`predict_proba`, :meth:`decision_path` and :meth:`apply`. All
        these methods are parallelized over the trees in the forets. ``n_jobs=-1``
        means using all processors.

    random_state : int, RandomState instance or None, default=None
        Controls both the randomness of the bootstrapping of the samples used
        when building trees (if ``bootstrap=True``) and the sampling of the
        features to consider when looking for the best split at each node
        (if ``max_features < n_features``).
        See :term:`Glossary <random_state>` for details.

    verbose : int, default=0
        Controls the verbosity when fitting and predicting.

    class_weight : {"balanced", "balanced_subsample"}, dict or list of dicts, \
            default=None
        Weights associated with classes in the form ``{class_label: weight}``.
        If not given, all classes are supposed to have weight one. For
        multi-output problems, a list of dicts can be provided in the same
        order as the columns of y.

        Note that for multioutput (including multilabel) weights should be
        defined for each class of every column in its own dict. For example,
        for four-class multilabel classification weights should be
        [{0: 1, 1: 1}, {0: 1, 1: 5}, {0: 1, 1: 1}, {0: 1, 1: 1}] instead of
        [{1:1}, {2:5}, {3:1}, {4:1}].

        The "balanced" mode uses the values of y to automatically adjust
        weights inversely proportional to class frequencies in the input data
        as ``n_samples / (n_classes * np.bincount(y))``

        The "balanced_subsample" mode is the same as "balanced" except that
        weights are computed based on the bootstrap sample for every tree
        grown.

        For multi-output, the weights of each column of y will be multiplied.

        Note that these weights will be multiplied with sample_weight (passed
        through the fit method) if sample_weight is specified.

    References
    ----------
    TODO: insert references

    """

    def __init__(
        self,
        *,
        n_estimators=100,
        criterion="gini",
        loss="log",
        step=1.0,
        aggregation=True,
        dirichlet=0.5,
        max_depth=None,
        min_samples_split=2,
        min_samples_leaf=1,
        max_bins=255,
        categorical_features=None,
        max_features="auto",
        # TODO: default for n_jobs must be integer ?
        n_jobs=1,
        random_state=None,
        verbose=False,
        class_weight=None,
    ):
        self._fitted = False
        self._n_features_ = None
        self._n_features_ = None
        self._n_outputs_ = 1

        self.n_estimators = n_estimators
        self.criterion = criterion
        self.loss = loss
        self.step = step
        self.aggregation = aggregation
        self.dirichlet = dirichlet
        self.max_depth = max_depth
        self.min_samples_split = min_samples_split
        self.min_samples_leaf = min_samples_leaf
        self.max_bins = max_bins
        self.categorical_features = categorical_features
        self.max_features = max_features
        self.n_jobs = n_jobs
        self.random_state = random_state
        self.verbose = verbose
        self.class_weight = class_weight

    def _encode_y(self, y):
        # encode classes into 0 ... n_classes - 1 and sets attributes classes_
        # and n_trees_per_iteration_
        check_classification_targets(y)

        label_encoder = LabelEncoder()
        encoded_y = label_encoder.fit_transform(y)
        self.classes_ = label_encoder.classes_
        n_classes = self.classes_.shape[0]
        # only 1 tree for binary classification. For multiclass classification,
        # we build 1 tree per class.
        self.n_trees_per_iteration_ = 1 if n_classes <= 2 else n_classes
        encoded_y = encoded_y.astype(np.float64, copy=False)
        return encoded_y

    def fit(self, X, y):
        """
        Build a forest of trees from the training set (X, y).

        Parameters
        ----------
        X : {array-like, sparse matrix} of shape (n_samples, n_features)
            The training input samples. Internally, its dtype will be converted
            to ``dtype=np.float32``. If a sparse matrix is provided, it will be
            converted into a sparse ``csc_matrix``.

        y : array-like of shape (n_samples,) or (n_samples, n_outputs)
            The target values (class labels in classification, real numbers in
            regression).

        Returns
        -------
        self : object
        """
        # Validate or convert input data

        # TODO: reprendre ce que j'avais dans _classes.py et gerer la creation des bins
        # TODO: tout simplifier au cas classification binaire
        # TODO : on a fait un copier/coller depuis scikit-learn ici...

        # TODO: Why only float64 ? What is the data is already binned ?
        X, y = self._validate_data(X, y, dtype=[np.float64], force_all_finite=False)
        y = self._encode_y(y)
        check_consistent_length(X, y)

        # Do not create unit sample weights by default to later skip some
        # computation
        # if sample_weight is not None:
        #     sample_weight = _check_sample_weight(sample_weight, X, dtype=np.float64)
        #     # TODO: remove when PDP suports sample weights
        #     self._fitted_with_sw = True

        rng = check_random_state(self.random_state)

        # # When warm starting, we want to re-use the same seed that was used
        # # the first time fit was called (e.g. for subsampling or for the
        # # train/val split).
        # if not (self.warm_start and self._is_fitted()):
        #     self._random_seed = rng.randint(np.iinfo(np.uint32).max, dtype="u8")

        # TODO: mettre un max de trucs dans les properties
        # self._validate_parameters()

        # used for validation in predict
        n_samples, self._n_features = X.shape

        self.is_categorical_, known_categories = self._check_categories(X)

        # we need this stateful variable to tell raw_predict() that it was
        # called from fit() (this current method), and that the data it has
        # received is pre-binned.
        # predicting is faster on pre-binned data, so we want early stopping
        # predictions to be made on pre-binned data. Unfortunately the _scorer
        # can only call predict() or predict_proba(), not raw_predict(), and
        # there's no way to tell the scorer that it needs to predict binned
        # data.
        self._in_fit = True

        # if isinstance(self.loss, str):
        #     self._loss = self._get_loss(sample_weight=sample_weight)
        # elif isinstance(self.loss, BaseLoss):
        #     self._loss = self.loss

        # if self.early_stopping == "auto":
        #     self.do_early_stopping_ = n_samples > 10000
        # else:
        #     self.do_early_stopping_ = self.early_stopping

        # self._use_validation_data = self.validation_fraction is not None
        # if self.do_early_stopping_ and self._use_validation_data:
        #     # stratify for classification
        #     stratify = y if hasattr(self._loss, "predict_proba") else None

        # TODO: bootstrap avec stratification pour jeux de donnees mal balances ?
        #  Quelle est la bonne facon de gerer ca ?

        # Save the state of the RNG for the training and validation split.
        # This is needed in order to have the same split when using
        # warm starting.
        #     if sample_weight is None:
        #         X_train, X_val, y_train, y_val = train_test_split(
        #             X,
        #             y,
        #             test_size=self.validation_fraction,
        #             stratify=stratify,
        #             random_state=self._random_seed,
        #         )
        #         sample_weight_train = sample_weight_val = None
        #     else:
        #         # TODO: incorporate sample_weight in sampling here, as well as
        #         # stratify
        #         (
        #             X_train,
        #             X_val,
        #             y_train,
        #             y_val,
        #             sample_weight_train,
        #             sample_weight_val,
        #         ) = train_test_split(
        #             X,
        #             y,
        #             sample_weight,
        #             test_size=self.validation_fraction,
        #             stratify=stratify,
        #             random_state=self._random_seed,
        #         )
        # else:
        #     X_train, y_train, sample_weight_train = X, y, sample_weight
        #     X_val = y_val = sample_weight_val = None

        # TODO: bon il faut gerer: 1. le bootstrap 2. la stratification 3. les
        #  sample_weight 4. le binning. On va pour l'instant aller au plus simple: 1.
        #  on binne les donnees des de debut et on ne s'emmerde pas avec les
        #  sample_weight. Le bootstrap et la stratification devront etre geres dans la
        #  fonction _generate_train_valid_samples au dessus (qu'on appelle avec un
        #  Parallel comme dans l'implem originelle des forets)

        # Bin the data
        # For ease of use of the API, the user-facing GBDT classes accept the
        # parameter max_bins, which doesn't take into account the bin for
        # missing values (which is always allocated). However, since max_bins
        # isn't the true maximal number of bins, all other private classes
        # (binmapper, histbuilder...) accept n_bins instead, which is the
        # actual total number of bins. Everywhere in the code, the
        # convention is that n_bins == max_bins + 1
        n_bins = self.max_bins + 1  # + 1 for missing values

        # TODO: ici ici ici faut que je reprenne le code de scikit et pas de pygbm
        #  car ils gerent les features categorielles dans le mapper

        self._bin_mapper = Binner(
            n_bins=n_bins,
            is_categorical=self.is_categorical_,
            known_categories=known_categories,
            # TODO: what to do with this ?
            # random_state=self._random_seed,
        )
        X_binned = self._bin_data(X, is_training_data=True)

        # X_binned_train = self._bin_data(X_train, is_training_data=True)
        # if X_val is not None:
        #     X_binned_val = self._bin_data(X_val, is_training_data=False)
        # else:
        #     X_binned_val = None

        # Uses binned data to check for missing values
        has_missing_values = (
            (X_binned == self._bin_mapper.missing_values_bin_idx_)
            .any(axis=0)
            .astype(np.uint8)
        )

        # if self.verbose:
        #     print("Fitting gradient boosted rounds:")

        n_samples = X_binned.shape[0]

        # if not self.warm_start or not hasattr(self, "estimators_"):
        #     # Free allocated memory, if any

        # TODO: ici on initialise les estimateurs. Gerer le warm-start plus tard
        # self.estimators_ = []

        # print("self.n_estimators: ", self.n_estimators)

        trees = [
            TreeBinaryClassifier(
                criterion=self.criterion,
                loss=self.loss,
                step=self.step,
                aggregation=self.aggregation,
                dirichlet=self.dirichlet,
                max_depth=self.max_depth,
                min_samples_split=self.min_samples_split,
                min_samples_leaf=self.min_samples_leaf,
                categorical_features=self.categorical_features,
                max_features=self.max_features,
                random_state=self.random_state,
                verbose=self.verbose,
            )
            for _ in range(self.n_estimators)
        ]

        # trees = [
        #     self._make_estimator(
        #         append=False,
        #         # TODO: correct random_state here ?
        #         random_state=rng,
        #     )
        #     for i in range(self.n_estimators)
        # ]

        # Parallel loop: we prefer the threading backend as the Cython code
        # for fitting the trees is internally releasing the Python GIL
        # making threading more efficient than multiprocessing in
        # that case. However, for joblib 0.12+ we respect any
        # parallel_backend contexts set at a higher level,
        # since correctness does not rely on using threads.

        print("self.n_jobs: ", self.n_jobs)

        print("options: ", _joblib_parallel_args(prefer="threads"))
        trees = Parallel(
            n_jobs=self.n_jobs,
            # verbose=self.verbose,
            # verbose=10,
            # backend="threading",
            # require="sharedmem"
            **_joblib_parallel_args(prefer="threads"),
        )(
            delayed(_parallel_build_trees)(
                # tree, X, y, sample_weight, i, len(trees),
                tree,
                X_binned,
                y,
                # TODO: deal with sample_weight
                None,
                tree_idx,
                len(trees),
                verbose=self.verbose,
            )
            for tree_idx, tree in enumerate(trees)
        )

        self.trees = trees
        self._fitted = True

        return self

    def get_nodes(self, tree_idx):
        return self.trees[tree_idx].get_nodes()

    def _validate_y_class_weight(self, y):
        # TODO: c'est un copier / coller de scikit. Simplifier au cas classif binaire
        check_classification_targets(y)

        y = np.copy(y)
        expanded_class_weight = None

        if self.class_weight is not None:
            y_original = np.copy(y)

        self.classes_ = []
        self.n_classes_ = []

        y_store_unique_indices = np.zeros(y.shape, dtype=int)
        for k in range(self.n_outputs_):
            classes_k, y_store_unique_indices[:, k] = np.unique(
                y[:, k], return_inverse=True
            )
            self.classes_.append(classes_k)
            self.n_classes_.append(classes_k.shape[0])
        y = y_store_unique_indices

        if self.class_weight is not None:
            valid_presets = ("balanced", "balanced_subsample")
            if isinstance(self.class_weight, str):
                if self.class_weight not in valid_presets:
                    raise ValueError(
                        "Valid presets for class_weight include "
                        '"balanced" and "balanced_subsample".'
                        'Given "%s".' % self.class_weight
                    )
                if self.warm_start:
                    warn(
                        'class_weight presets "balanced" or '
                        '"balanced_subsample" are '
                        "not recommended for warm_start if the fitted data "
                        "differs from the full dataset. In order to use "
                        '"balanced" weights, use compute_class_weight '
                        '("balanced", classes, y). In place of y you can use '
                        "a large enough sample of the full training set "
                        "target to properly estimate the class frequency "
                        "distributions. Pass the resulting weights as the "
                        "class_weight parameter."
                    )

            if self.class_weight != "balanced_subsample" or not self.bootstrap:
                if self.class_weight == "balanced_subsample":
                    class_weight = "balanced"
                else:
                    class_weight = self.class_weight
                expanded_class_weight = compute_sample_weight(class_weight, y_original)

        return y, expanded_class_weight

    def _check_categories(self, X):
        """Check and validate categorical features in X

        Return
        ------
        is_categorical : ndarray of shape (n_features,) or None, dtype=bool
            Indicates whether a feature is categorical. If no feature is
            categorical, this is None.
        known_categories : list of size n_features or None
            The list contains, for each feature:
                - an array of shape (n_categories,) with the unique cat values
                - None if the feature is not categorical
            None if no feature is categorical.
        """
        if self.categorical_features is None:
            return None, None

        categorical_features = np.asarray(self.categorical_features)

        if categorical_features.size == 0:
            return None, None

        if categorical_features.dtype.kind not in ("i", "b"):
            raise ValueError(
                "categorical_features must be an array-like of "
                "bools or array-like of ints."
            )

        n_features = X.shape[1]

        # check for categorical features as indices
        if categorical_features.dtype.kind == "i":
            if (
                np.max(categorical_features) >= n_features
                or np.min(categorical_features) < 0
            ):
                raise ValueError(
                    "categorical_features set as integer "
                    "indices must be in [0, n_features - 1]"
                )
            is_categorical = np.zeros(n_features, dtype=bool)
            is_categorical[categorical_features] = True
        else:
            if categorical_features.shape[0] != n_features:
                raise ValueError(
                    "categorical_features set as a boolean mask "
                    "must have shape (n_features,), got: "
                    f"{categorical_features.shape}"
                )
            is_categorical = categorical_features

        if not np.any(is_categorical):
            return None, None

        # compute the known categories in the training data. We need to do
        # that here instead of in the BinMapper because in case of early
        # stopping, the mapper only gets a fraction of the training data.
        known_categories = []

        for f_idx in range(n_features):
            if is_categorical[f_idx]:
                categories = np.unique(X[:, f_idx])
                missing = np.isnan(categories)
                if missing.any():
                    categories = categories[~missing]

                if categories.size > self.max_bins:
                    raise ValueError(
                        f"Categorical feature at index {f_idx} is "
                        f"expected to have a "
                        f"cardinality <= {self.max_bins}"
                    )

                if (categories >= self.max_bins).any():
                    raise ValueError(
                        f"Categorical feature at index {f_idx} is "
                        f"expected to be encoded with "
                        f"values < {self.max_bins}"
                    )
            else:
                categories = None
            known_categories.append(categories)

        return is_categorical, known_categories

    def _bin_data(self, X, is_training_data):
        """Bin data X.

        If is_training_data, then fit the _bin_mapper attribute.
        Else, the binned data is converted to a C-contiguous array.
        """

        description = "training" if is_training_data else "validation"
        if self.verbose:
            print(
                "Binning {:.3f} GB of {} data: ".format(X.nbytes / 1e9, description),
                end="",
                flush=True,
            )
        # tic = time()
        if is_training_data:
            X_binned = self._bin_mapper.fit_transform(X)  # F-aligned array
        else:
            X_binned = self._bin_mapper.transform(X)  # F-aligned array
            # We convert the array to C-contiguous since predicting is faster
            # with this layout (training is faster on F-arrays though)
            X_binned = np.ascontiguousarray(X_binned)
        # toc = time()
        # if self.verbose:
        #     duration = toc - tic
        #     print("{:.3f} s".format(duration))

        return X_binned

    def predict(self, X):
        """
        Predict class for X.

        The predicted class of an input sample is a vote by the trees in
        the forest, weighted by their probability estimates. That is,
        the predicted class is the one with highest mean probability
        estimate across the trees.

        Parameters
        ----------
        X : {array-like, sparse matrix} of shape (n_samples, n_features)
            The input samples. Internally, its dtype will be converted to
            ``dtype=np.float32``. If a sparse matrix is provided, it will be
            converted into a sparse ``csr_matrix``.

        Returns
        -------
        y : ndarray of shape (n_samples,) or (n_samples, n_outputs)
            The predicted classes.
        """

        # TODO: c'est un copier / coller de scikit-learn. Simplifier au cas
        #  classification binaire. Et il faut binner les features avant de predire

        proba = self.predict_proba(X)

        if self.n_outputs_ == 1:
            return self.classes_.take(np.argmax(proba, axis=1), axis=0)

        else:
            n_samples = proba[0].shape[0]
            # all dtypes should be the same, so just take the first
            class_type = self.classes_[0].dtype
            predictions = np.empty((n_samples, self.n_outputs_), dtype=class_type)

            for k in range(self.n_outputs_):
                predictions[:, k] = self.classes_[k].take(
                    np.argmax(proba[k], axis=1), axis=0
                )

            return predictions

    def predict_proba(self, X):
        """
        Predict class probabilities for X.

        The predicted class probabilities of an input sample are computed as
        the mean predicted class probabilities of the trees in the forest.
        The class probability of a single tree is the fraction of samples of
        the same class in a leaf.

        Parameters
        ----------
        X : {array-like, sparse matrix} of shape (n_samples, n_features)
            The input samples. Internally, its dtype will be converted to
            ``dtype=np.float32``. If a sparse matrix is provided, it will be
            converted into a sparse ``csr_matrix``.

        Returns
        -------
        p : ndarray of shape (n_samples, n_classes), or a list of n_outputs
            such arrays if n_outputs > 1.
            The class probabilities of the input samples. The order of the
            classes corresponds to that in the attribute :term:`classes_`.
        """
        # TODO: c'est un copier / coller de scikit-learn. Simplifier au cas
        #  classification binaire. Et il faut binner les features avant de predire

        check_is_fitted(self)
        # Check data
        X = self._validate_X_predict(X)

        # TODO: we can also avoid data binning for predictions...
        # Bin the data
        X_binned = self._bin_data(X, is_training_data=False)

        # Assign chunk of trees to jobs
        n_jobs, _, _ = _partition_estimators(self.n_estimators, self.n_jobs)

        # TODO: on ne gere pas encore le cas multi-output mais juste un label binaire
        # avoid storing the output of every estimator by summing them here
        # all_proba = [
        #     np.zeros((X.shape[0], j), dtype=np.float64)
        #     for j in np.atleast_1d(self.n_classes_)
        # ]

        all_proba = np.zeros((X_binned.shape[0], 2))

        lock = threading.Lock()
        Parallel(
            n_jobs=n_jobs,
            verbose=self.verbose,
            **_joblib_parallel_args(require="sharedmem"),
        )(
            delayed(_accumulate_prediction)(e.predict_proba, X_binned, all_proba, lock)
            for e in self.trees
        )

        # for proba in all_proba:
        #     proba /= len(self.trees)
        all_proba /= len(self.trees)

        # if len(all_proba) == 1:
        #     return all_proba[0]
        # else:
        #     return all_proba
        return all_proba

    def predict_proba_trees(self, X):
        check_is_fitted(self)
        # Check data
        X = self._validate_X_predict(X)
        # TODO: we can also avoid data binning for predictions...
        X_binned = self._bin_data(X, is_training_data=False)
        n_samples, n_features = X.shape
        n_estimators = len(self.trees)
        n_jobs, _, _ = _partition_estimators(self.n_estimators, self.n_jobs)
        probas = np.empty((n_estimators, n_samples, n_features))

        lock = threading.Lock()
        Parallel(
            n_jobs=n_jobs,
            verbose=self.verbose,
            **_joblib_parallel_args(require="sharedmem"),
        )(
            delayed(_get_tree_prediction)(e.predict_proba, X_binned, probas, lock, tree_idx)
            for tree_idx, e in enumerate(self.trees)
        )
        return probas
        # return self.trees[tree_idx].predict_proba(X)

    def predict_log_proba(self, X):
        """
        Predict class log-probabilities for X.

        The predicted class log-probabilities of an input sample is computed as
        the log of the mean predicted class probabilities of the trees in the
        forest.

        Parameters
        ----------
        X : {array-like, sparse matrix} of shape (n_samples, n_features)
            The input samples. Internally, its dtype will be converted to
            ``dtype=np.float32``. If a sparse matrix is provided, it will be
            converted into a sparse ``csr_matrix``.

        Returns
        -------
        p : ndarray of shape (n_samples, n_classes), or a list of n_outputs
            such arrays if n_outputs > 1.
            The class probabilities of the input samples. The order of the
            classes corresponds to that in the attribute :term:`classes_`.
        """
        # TODO: c'est un copier / coller de scikit-learn. Simplifier au cas
        #  classification binaire. Et il faut binner les features avant de predire

        proba = self.predict_proba(X)

        if self.n_outputs_ == 1:
            return np.log(proba)

        else:
            for k in range(self.n_outputs_):
                proba[k] = np.log(proba[k])

            return proba

    def apply(self, X):
        """
        Apply trees in the forest to X, return leaf indices.

        Parameters
        ----------
        X : {array-like, sparse matrix} of shape (n_samples, n_features)
            The input samples. Internally, its dtype will be converted to
            ``dtype=np.float32``. If a sparse matrix is provided, it will be
            converted into a sparse ``csr_matrix``.

        Returns
        -------
        X_leaves : ndarray of shape (n_samples, n_estimators)
            For each datapoint x in X and for each tree in the forest,
            return the index of the leaf x ends up in.
        """
        X = self._validate_X_predict(X)
        results = Parallel(
            n_jobs=self.n_jobs,
            verbose=self.verbose,
            **_joblib_parallel_args(prefer="threads"),
        )(delayed(tree.apply)(X, check_input=False) for tree in self.estimators_)

        return np.array(results).T

    def decision_path(self, X):
        """
        Return the decision path in the forest.

        .. versionadded:: 0.18

        Parameters
        ----------
        X : {array-like, sparse matrix} of shape (n_samples, n_features)
            The input samples. Internally, its dtype will be converted to
            ``dtype=np.float32``. If a sparse matrix is provided, it will be
            converted into a sparse ``csr_matrix``.

        Returns
        -------
        indicator : sparse matrix of shape (n_samples, n_nodes)
            Return a node indicator matrix where non zero elements indicates
            that the samples goes through the nodes. The matrix is of CSR
            format.

        n_nodes_ptr : ndarray of shape (n_estimators + 1,)
            The columns from indicator[n_nodes_ptr[i]:n_nodes_ptr[i+1]]
            gives the indicator value for the i-th estimator.

        """
        X = self._validate_X_predict(X)
        indicators = Parallel(
            n_jobs=self.n_jobs,
            verbose=self.verbose,
            **_joblib_parallel_args(prefer="threads"),
        )(
            delayed(tree.decision_path)(X, check_input=False)
            for tree in self.estimators_
        )

        n_nodes = [0]
        n_nodes.extend([i.shape[1] for i in indicators])
        n_nodes_ptr = np.array(n_nodes).cumsum()

        return sparse_hstack(indicators).tocsr(), n_nodes_ptr

    def _validate_y_class_weight(self, y):
        # Default implementation
        return y, None

    def _validate_X_predict(self, X):
        """
        Validate X whenever one tries to predict, apply, predict_proba."""
        check_is_fitted(self)

        return self.trees[0]._validate_X_predict(X, check_input=True)

    @property
    def n_features_(self):
        if self._fitted:
            return self._n_features_
        else:
            raise ValueError("You must call fit before asking for n_features_")

    @n_features_.setter
    def n_features_(self, val):
        raise ValueError("n_features_ is a readonly attribute")

    @property
    def n_estimators(self):
        return self._n_estimators

    @n_estimators.setter
    def n_estimators(self, val):
        if self._fitted:
            raise ValueError("You cannot change n_estimators after calling fit")
        else:
            if not isinstance(val, numbers.Integral):
                raise ValueError("n_estimators must an integer number")
            elif val < 1:
                raise ValueError("n_estimators must be >= 1")
            else:
                self._n_estimators = val

    @property
    def n_jobs(self):
        return self._n_jobs

    @n_jobs.setter
    def n_jobs(self, val):
        if not isinstance(val, numbers.Integral):
            raise ValueError("n_jobs must be an integer number")
        elif val < -1:
            raise ValueError("n_jobs must be >= -1")
        else:
            self._n_jobs = int(val)

    @property
    def step(self):
        return self._step

    @step.setter
    def step(self, val):
        if not isinstance(val, numbers.Real):
            raise ValueError("step must be a real number")
        elif val <= 0:
            raise ValueError("step must be positive")
        else:
            self._step = float(val)

    @property
    def aggregation(self):
        return self._aggregation

    @aggregation.setter
    def aggregation(self, val):
        if not isinstance(val, bool):
            raise ValueError("aggregation must be boolean")
        else:
            self._aggregation = val

    @property
    def verbose(self):
        return self._verbose

    @verbose.setter
    def verbose(self, val):
        if not isinstance(val, bool):
            raise ValueError("`verbose` must be of type `bool`")
        else:
            self._verbose = val

    @property
    def loss(self):
        return "log"

    @loss.setter
    def loss(self, val):
        pass

    @property
    def random_state(self):
        """:obj:`int` or :obj:`None`: Controls the randomness involved in the trees."""
        if self._random_state == -1:
            return None
        else:
            return self._random_state

    @random_state.setter
    def random_state(self, val):
        if self._fitted:
            raise ValueError("You cannot modify `random_state` after calling `fit`")
        else:
            if val is None:
                self._random_state = -1
            elif not isinstance(val, int):
                raise ValueError("`random_state` must be of type `int`")
            elif val < 0:
                raise ValueError("`random_state` must be >= 0")
            else:
                self._random_state = val

    # TODO: code properties for all arguments with scikit-learn checks
