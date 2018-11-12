# Copyright 2018 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You may not
# use this file except in compliance with the License. A copy of the License
# is located at
#
#     http://aws.amazon.com/apache2.0/
#
# or in the "license" file accompanying this file. This file is distributed on
# an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either
# express or implied. See the License for the specific language governing
# permissions and limitations under the License.

"""

DataWig HPO
Implements hyperparameter optimisation for datawig


"""

import pandas as pd
import itertools
import time

from pandas.api.types import is_numeric_dtype
from sklearn.metrics import mean_squared_error, f1_score, precision_score, accuracy_score, recall_score

from .column_encoders import BowEncoder, CategoricalEncoder, NumericalEncoder, ColumnEncoder, TfIdfEncoder
from .mxnet_input_symbols import BowFeaturizer, NumericalFeaturizer, Featurizer, EmbeddingFeaturizer
from .utils import logger, get_context, random_split, rand_string, flatten_dict, merge_dicts


class HPO:
    """
    Implements systematic hyperparameter optimisation for datawig

    Example usage:

    imputer = SimpleImputer(input_columns, output_column)
    hps = dict( ... )  # specify hyperparameter choices
    hpo = HPO(impter, hps)
    results = hpo.tune

    """

    def __init__(self, imputer):

        """
        Init method also defines default hyperparameter choices, global and for each input column type.

        :param imputer: SimpleImputer instance with input_colums and output_column and output_path specified.

        """

        self.imputer = imputer
        self.output_path = imputer.output_path
        self.hps = None
        self.results = pd.DataFrame()

        # Define default hyperparameter choices for each column type (string, categorical, numeric)
        default_hps = dict()
        default_hps['global'] = {}
        default_hps['global']['learning_rate'] = [1e-4, 1e-3]
        default_hps['global']['weight_decay'] = [0, 1e-4]
        default_hps['global']['num_epochs'] = [100]
        default_hps['global']['patience'] = [5]
        default_hps['global']['batch_size'] = [16]
        default_hps['global']['final_fc_hidden_units'] = [[], [100]]
        default_hps['global']['concat_columns'] = [True, False]

        default_hps['string'] = {}
        default_hps['string']['ngram_range'] = {}
        default_hps['string']['max_tokens'] = [2 ** 8]
        default_hps['string']['tokens'] = [['words']]
        default_hps['string']['ngram_range']['words'] = [(1, 3)]
        default_hps['string']['ngram_range']['chars'] = [(1, 5)]

        default_hps['categorical'] = {}
        default_hps['categorical']['max_tokens'] = [2 ** 8]
        default_hps['categorical']['embed_dim'] = [10]

        default_hps['numeric'] = {}
        default_hps['numeric']['normalize'] = [True]
        default_hps['numeric']['numeric_latent_dim'] = [10, 50]
        default_hps['numeric']['numeric_hidden_layers'] = [1, 2]

        # parameters for a single column of concatenated strings
        default_hps['concat'] = default_hps['string'].copy()

        self.default_hps = default_hps

    def __preprocess_hps(self, train_df: pd.DataFrame) -> pd.DataFrame:
        """
        Generates list of all possible combinations of hyperparameter from the nested hp dictionary.
        Requires the data to check whether the relevant columns are present and have the appropriate type.

        :param train_df: training data as dataframe

        :return: Data frame where each row is a hyperparameter configuration and each column is a parameter.
                    Column names have the form colum:parameter, e.g. title:max_tokens or global:learning rate.
        """

        # create empty dict if global hps not passed
        if 'global' not in self.hps.keys():
            self.hps['global'] = {}

        # create empty dict if parameters for concatenated column not passed
        if 'concat' not in self.hps.keys():
            self.hps['concat'] = {}

        # add type to column dictionaries if it was not specified, does not support categorical types
        for column_name in self.imputer.input_columns:
            if 'type' not in self.hps[column_name].keys():
                if is_numeric_dtype(train_df[column_name]):
                    self.hps[column_name]['type'] = ['numeric']
                else:
                    self.hps[column_name]['type'] = ['string']

        # check that all passed parameter are valid hyperparameters
        assert all([key in self.default_hps['global'].keys() for key in self.hps['global'].keys()])
        for key, val in self.hps.items():
            if 'type' in val.keys():
                assert all([key in list(self.default_hps[val['type'][0]].keys()) + ['type'] for key, _ in val.items()])

        # augment provided global self.hps with default global hps so that cartesian products are full parameter sets.
        # self.hps['global'] = {**self.default_hps['global'], **self.hps['global']}
        self.hps['global'] = merge_dicts(self.default_hps['global'], self.hps['global'])
        self.hps['concat'] = merge_dicts(self.default_hps['concat'], self.hps['concat'])

        # augment provided self.hps with default self.hps, iterating over every input column
        for column_name in self.imputer.input_columns:
            # add dictionary for input columns where no hyperparamters have been passed
            if column_name not in self.hps.keys():
                self.hps[column_name] = {}
                if column_name in self.imputer.numeric_columns:
                    self.hps[column_name]['type'] = ['numeric']
                elif column_name in self.imputer.string_columns:
                    self.hps[column_name]['type'] = ['string']
                else:
                    logger.warn('Input type of column ' + str(column_name) + ' not determined.')
            # join all column specific hp dictionaries with type-specific default values
            # self.hps[column_name] = {**self.default_hps[self.hps[column_name]['type'][0]], **self.hps[column_name]}
            self.hps[column_name] = merge_dicts(self.default_hps[self.hps[column_name]['type'][0]],
                                                self.hps[column_name])

        # flatten nested dictionary structures and combine to data frame with all possible hp configurations
        hp_df_from_dict = lambda dict: pd.DataFrame(list(itertools.product(*dict.values())), columns=dict.keys())

        return hp_df_from_dict(flatten_dict(self.hps))

    def __fit_hp(self,
                 train_df: pd.DataFrame,
                 test_df: pd.DataFrame,
                 hp: pd.Series,
                 user_defined_scores: list = None,
                 name: str = ''):
        """

        Method initialises the model, performs fitting and returns the desired metrics.


        :param train_df: training data as dataframe
        :param test_df: test data as dataframe; if not provided, a ratio of test_split of the
                          training data are used as test data

        :param hp: pd.Series with hyperparameter configuration
        :param user_defined_scores: list with entries (Callable, str), where callable is a function
                          accepting **kwargs true, predicted, confidence. Allows custom scoring functions.
        :param name to identify the current setting of hps.

        :return:

        """

        data_encoders = []
        data_featurizers = []

        if hp['global:concat_columns'] is False:

            # define column encoders and featurisers for each input column
            for input_column in self.imputer.input_columns:

                # extract parameters for the current input column, take everything after the first colon
                col_parms = {':'.join(key.split(':')[1:]): val for key, val in hp.items() if input_column in key}

                # define all input columns
                if col_parms['type'] == 'string':
                    # iterate over multiple embeddings (chars + strings for the same column)
                    for token in col_parms['tokens']:
                        # call kw. args. with: **{key: item for key, item in col_parms.items() if not key == 'type'})]
                        data_encoders += [TfIdfEncoder(input_columns=[input_column],
                                                       output_column=input_column + '_' + token,
                                                       tokens=token,
                                                       ngram_range=col_parms['ngram_range:'+token],
                                                       max_tokens=col_parms['max_tokens'])]
                        data_featurizers += [BowFeaturizer(field_name=input_column + '_' + token,
                                                           max_tokens=col_parms['max_tokens'])]

                elif col_parms['type'] == 'categorical':
                    data_encoders += [CategoricalEncoder(input_columns=[input_column],
                                                         output_column=input_column + '_' + col_parms['type'],
                                                         max_tokens=col_parms['max_tokens'])]
                    data_featurizers += [EmbeddingFeaturizer(field_name=input_column + '_' + col_parms['type'],
                                                             max_tokens=col_parms['max_tokens'],
                                                             embed_dim=col_parms['embed_dim'])]

                elif col_parms['type'] == 'numeric':
                    data_encoders += [NumericalEncoder(input_columns=[input_column],
                                                       output_column=input_column + '_' + col_parms['type'],
                                                       normalize=col_parms['normalize'])]
                    data_featurizers += [NumericalFeaturizer(field_name=input_column + '_' + col_parms['type'],
                                                             numeric_latent_dim=col_parms['numeric_latent_dim'],
                                                             numeric_hidden_layers=col_parms['numeric_hidden_layers'])]
                else:
                    logger.warn('Found unknown column type. Canidates are string, categorical, numeric.')

        # Concatenate all columns
        else:
            # cast all columns to string for concatenation
            train_df = train_df.astype(str)
            test_df = test_df.astype(str)

            col_parms = {':'.join(key.split(':')[1:]): val for key, val in hp.items() if 'concat' in key}
            for token in col_parms['tokens']:
                data_encoders += [TfIdfEncoder(input_columns=self.imputer.input_columns,
                                               output_column='-'.join(self.imputer.input_columns) + '_' + token,
                                               tokens=token,
                                               ngram_range=col_parms['ngram_range:' + token],
                                               max_tokens=col_parms['max_tokens'])]
                data_featurizers += [BowFeaturizer(field_name='-'.join(self.imputer.input_columns) + '_' + token,
                                                   max_tokens=col_parms['max_tokens'])]

        # Define separate encoder and featurizer for each column
        # Define output column. Associated parameters are not tuned.
        if is_numeric_dtype(train_df[self.imputer.output_column]):
            label_column = [NumericalEncoder(self.imputer.output_column)]
            logger.info("Assuming numeric output column: {}".format(self.imputer.output_column))
        else:
            label_column = [CategoricalEncoder(self.imputer.output_column)]
            logger.info("Assuming categorical output column: {}".format(self.imputer.output_column))

        global_parms = {key.split(':')[1]: val for key, val in hp.iteritems() if 'global' in key}

        from . import Imputer  # needs to be imported here to avoid circular dependency
        hp_imputer = Imputer(data_encoders=data_encoders,
                             data_featurizers=data_featurizers,
                             label_encoders=label_column,
                             output_path=self.output_path + name)

        print('\n\n\n')
        print(hp)
        print('\n\n\n')

        hp_imputer.fit(train_df=train_df,
                       test_df=test_df,
                       ctx=get_context(),
                       learning_rate=global_parms['learning_rate'],
                       num_epochs=global_parms['num_epochs'],
                       patience=global_parms['patience'],
                       test_split=.1,
                       weight_decay=global_parms['weight_decay'],
                       batch_size=global_parms['batch_size'],
                       final_fc_hidden_units=global_parms['final_fc_hidden_units'],
                       calibrate=True)

        # add suitable metrics to hp series
        imputed = hp_imputer.predict(test_df)
        true = imputed[self.imputer.output_column]
        predicted = imputed[self.imputer.output_column + '_imputed']

        imputed_train = hp_imputer.predict(train_df.sample(min(train_df.shape[0], int(1e4))))
        true_train = imputed_train[self.imputer.output_column]
        predicted_train = imputed_train[self.imputer.output_column + '_imputed']

        if is_numeric_dtype(train_df[self.imputer.output_column]):
            hp['mse'] = mean_squared_error(true, predicted)
            hp['mse_train'] = mean_squared_error(true_train, predicted_train)
            confidence = float('nan')
        else:
            confidence = imputed[self.imputer.output_column + '_imputed_proba']
            confidence_train = imputed_train[self.imputer.output_column + '_imputed_proba']
            hp['f1_micro'] = f1_score(true, predicted, average='micro')
            hp['f1_macro'] = f1_score(true, predicted, average='macro')
            hp['f1_weighted'] = f1_score(true, predicted, average='weighted')
            hp['f1_weighted_train'] = f1_score(true_train, predicted_train, average='weighted')
            hp['precision_weighted'] = f1_score(true, predicted, average='weighted')
            hp['precision_weighted_train'] = f1_score(true_train, predicted_train, average='weighted')
            hp['recall_weighted'] = recall_score(true, predicted, average='weighted')
            hp['recall_weighted_train'] = recall_score(true_train, predicted_train, average='weighted')
            hp['coverage_at_90'] = (confidence > .9).mean()
            hp['coverage_at_90_train'] = (confidence_train > .9).mean()

        for uds in user_defined_scores:
            hp[uds[1]] = uds[0](true=true, predicted=predicted, confidence=confidence)

        return hp

    def tune(self,
             train_df: pd.DataFrame,
             test_df: pd.DataFrame = None,
             hps: dict = None,
             strategy: str = 'random',
             num_evals: int = 10,
             max_running_hours = 96,
             user_defined_scores: list = None,
             hpo_run_name: str = ''):

        """
        Do grid search or random search for hyperparameter configurations. This method can not tune tfidf vs hashing
        vectorization but uses tfidf. Also parameters of the output column encoder are not tuned.
        
        :param train_df: training data as dataframe
        :param test_df: test data as dataframe; if not provided, a ratio of test_split of the
                          training data are used as test data
        :param hps: nested dictionary where hps[global][parameter_name] is list of parameters. Similarly,
                          hps[column_name][parameter_name] is a list of parameter values for each input column.
                          Further, hps[column_name]['type'] is in ['numeric', 'categorical', 'string'] and is
                          inferred if not provided. See init method of HPO for further details.

        :param strategy: 'random' for random search or 'grid' for exhaustive search
        :param num_evals: number of evaluations for random search
        :param max_running_hours: Time before the hpo run is terminated in hours
        :param user_defined_scores: list with entries (Callable, str), where callable is a function
                          accepting **kwargs true, predicted, confidence. Allows custom scoring functions.
        :param hpo_run_name: Optional string identifier for this run.
                          Allows to sequentially run hpo jobs and keep previous iterations
    
        :return: None
        """

        if user_defined_scores is None:
            user_defined_scores = []

        self.hps = hps
        self.imputer.check_data_types(train_df)  # infer data types, saved self.string_columns, self.numeric_columns

        # process_hp_configurations(hps) uses self.hps to populate self.hps_flat
        hps_flat = self.__preprocess_hps(train_df)

        # train/test split if no test data given
        if test_df is None:
            train_df, test_df = random_split(train_df, [.8, .2])

        # sample configurations for random search
        if strategy == 'random':
            hps_flat = hps_flat.sample(n=min([num_evals, hps_flat.shape[0]]), random_state=10)

        logger.info("Training starts for " + str(hps_flat.shape[0]) + "hyperparameter configurations.")

        # iterate over hp configurations and fit models. This loop could be parallelized
        start_time = time.time()
        elapsed_time = 0

        for hp_idx, hp in hps_flat.iterrows():
            if elapsed_time > max_running_hours:
                logger.info('Finishing hpo because max running time was reached.')
                break

            # if concat_columns True, set all n.a. hps to n.a. TODO
            logger.info("Fitting hpo iteration " + str(hp_idx) + " with parameters\n\t" +
                        '\n\t'.join([str(i) + ': ' + str(j) for i, j in hp.items()]))
            name = hpo_run_name + str(hp_idx)

            # add results to hp
            hp = self.__fit_hp(train_df,
                               test_df,
                               hp,
                               user_defined_scores,
                               name)

            # append output to results data frame
            self.results = pd.concat([self.results, hp.to_frame(name).transpose()])

            logger.info('Finished hpo iteration ' + str(hp_idx))
            elapsed_time = (time.time() - start_time)/3600

    def load_model(self, hpo_idx: int = None, inplace: bool = True):
        """
        Load model after hyperparameter optimisation has ran.

        :param hpo_idx: Index of the model to be loaded. Default,
                    load model with highest weighted precision
        :param inplace: Selected model is assgined to the imputer object to the current model
                    if inplace is True and is returned otherwise.
        :return: imputer object
        """

        from . import Imputer  # import here to avoid circular dependency

        if hpo_idx is None:
            hpo_idx = self.results['precision_weighted'].idxmax()
            logger.info("Selecting imputer with maximum weighted precision.")

        if inplace is True:
            self.imputer.imputer = Imputer.load(self.output_path + str(hpo_idx))
        else:
            return Imputer.load(self.output_path + str(hpo_idx))
