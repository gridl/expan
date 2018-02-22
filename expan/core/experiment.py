import logging

import numpy as np
import pandas as pd

import expan.core.early_stopping as es
import expan.core.statistics as statx
from expan.core.statistical_test import *
from expan.core.util import get_kpi_by_name_and_variant
from expan.core.results import StatisticalTestResult, MultipleTestSuiteResult

logger = logging.getLogger(__name__)


class Experiment(object):
    """ Class which adds the analysis functions to experimental data. """
    def __init__(self, data, metadata):
        self.data         = data.copy()
        self.metadata     = metadata.copy()
        self.worker_table = {
            'fixed_horizon': statx.make_delta,
            'group_sequential': es.make_group_sequential,
            'bayes_factor': es.make_bayes_factor,
            'bayes_precision': es.make_bayes_precision
        }


    def __str__(self):
        return 'Experiment "{:s}" with {:d} entities.'.format(self.metadata['experiment'], len(self.data))


    #TODO: docstring
    def analyze_statistical_test(self, test, testmethod, **worker_args):
        if not isinstance(test, StatisticalTest):
            raise TypeError("Statistical test should be of type StatisticalTest.")

        if 'entity' not in self.data.columns():
            raise RuntimeError("There is no 'entity' column in the data.")
        if self.data.entity.duplicated().any():
            raise ValueError('Entities in data should be unique.')

        if test.variants.variant_column_name not in self.data.columns():
            raise RuntimeError("There is no '{}' column in the data.".format(test.variants.variant_column_name))
        if test.variants.treatment_name not in np.unique(self.data[test.variants.variant_column_name]):
            raise RuntimeError("There is no treatment with the name '{}' in the data.".format(test.variants.treatment_name))
        if test.variants.control_name not in np.unique(self.data[test.variants.variant_column_name]):
            raise RuntimeError("There is no control with the name '{}' in the data.".format(test.variants.control_name))

        if not isinstance(test.features, list):
            raise TypeError("Features should be a list.")
        if not all(isinstance(n, FeatureFilter) for n in test.features):
            raise TypeError("Some features are not of the type FeatureFilter.")
        for feature in test.features:
            if feature.column_name not in self.data.columns:
                raise RuntimeError("Feature name '{}' does not exist in the data.".format(feature.column_name))

        if type(test.kpi) is KPI and (test.kpi.name not in self.data.columns()):
            raise RuntimeError("There is no column of name '{}' in the data.".format(test.kpi.name))
        if type(test.kpi) is DerivedKPI:
            if type(test.kpi.formula) is not str:
                raise RuntimeError("Formula of derived KPI '{}' does not exist.".format(test.kpi.name))
            # create the derived kpi column if it is not yet created
            if not test.kpi.name in self.data.columns:
                test.kpi.make_derived_kpi(self.data)

        logger.info("One analysis with kpi '{}', control variant '{}', treatment variant '{}' and features [{}] "
                    "has just started".format(test.kpi_name, test.variants.control_name,
                                              test.variants.treatment_name,
                                              [(feature.column_name, feature.column_value) for feature in test.features]))

        if not testmethod in self.worker_table:
            raise NotImplementedError("Test method '{}' is not implemented.".format(testmethod))
        worker = self.worker_table[testmethod](**worker_args)

        data_for_analysis = self.data.copy()

        # create test result object with empty result first
        test_result = StatisticalTestResult(test, None)

        # apply feature filter to data
        for feature in test.features:
            data_for_analysis = data_for_analysis[feature.column_name == feature.column_value]

        if not self._is_valid_for_analysis(data_for_analysis, test):
            logger.warning("Data are not valid for analysis!")
            return test_result

        # get control and treatment values for the kpi
        control          = get_kpi_by_name_and_variant(data_for_analysis, test.kpi, test.variants.control_name)
        control_weight   = self._get_weights(data_for_analysis, test.kpi.name, test.variants.control_name)
        control_data     = control * control_weight

        treatment        = get_kpi_by_name_and_variant(data_for_analysis, test.kpi, test.variants.treatment_name)
        treatment_weight = self._get_weights(data_for_analysis, test.kpi.name, test.variants.treatment_name)
        treatment_data   = treatment * treatment_weight

        # run the test method
        test_statistics = worker(x=treatment_data, y=control_data)
        # TODO: implement power into the return value of worker()
        # power = statx.compute_statistical_power(treatment_data, control_data)
        # TODO: implement worker() returns an instance of child class of BaseTestStatistics

        test_result.result = test_statistics
        return test_result


    # TODO: Add docstring
    def analyze_statistical_test_suite(self, test_suite, testmethod='fixed_horizon', **worker_args):
        """
        Method runs delta analysis on a set of tests of the class StatisticalTestSuite and returns results for each
        statistical test in the suite along with the information about each test (instance of StatisticalTestSuite) 
        :param test_suite: instance of StatisticalTestSuite
        :param testmethod: testing method
        :param worker_args: additional arguments
        :return: instance of MultipleTestSuiteResult
        """
        if not isinstance(test_suite, StatisticalTestSuite):
            raise RuntimeError("Test suite should be of type StatisticalTestSuite.")

        statistical_test_results = MultipleTestSuiteResult([], test_suite.correction_method)
        for test in test_suite:
            one_analysis_result = self.analyze_statistical_test(test, testmethod, **worker_args)
            statistical_test_results.statistical_test_results.append(one_analysis_result)

        # TODO: implement correction method, create CorrectedTestStatistics, and update the statistical_test_results
        return statistical_test_results


    def outlier_filter(self, kpis, percentile=99.0, threshold_type='upper'):
        """ Method that filters out entities whose KPIs exceed the value at a given percentile.
        If any of the KPIs exceeds its threshold the entity is filtered out.
        :param kpis: list of KPI names
        :type  kpis: list[str]
        :param percentile: percentile considered as threshold
        :type  percentile: float
        :param threshold_type: type of threshold used ('lower' or 'upper')
        :type  threshold_type: str
        :returns: No return value. Will filter out outliers in self.data in place.
        """
        # check if provided KPIs are present in the data
        for kpi in kpis:
            if kpi not in self.data.columns:
                raise KeyError(kpi + ' identifier not present in dataframe columns!')
        # check if provided percentile is valid
        if 0.0 < percentile <= 100.0 is False:
            raise ValueError("Percentile value needs to be between 0.0 and 100.0!")
        # check if provided filtering kind is valid
        if threshold_type not in ['upper', 'lower']:
            raise ValueError("Threshold type needs to be either 'upper' or 'lower'!")

        # run quantile filtering
        flags = self._quantile_filtering(kpis=kpis, percentile=percentile, threshold_type=threshold_type)
        # log which columns were filtered and how many entities were filtered out
        self.metadata['filtered_columns'] = kpis
        self.metadata['filtered_entities_number'] = len(flags[flags == True])
        self.metadata['filtered_threshold_kind'] = threshold_type
        # throw warning if too many entities have been filtered out
        if (len(flags[flags == True]) / float(len(self.data))) > 0.02:
            logger.warning('More than 2% of entities have been filtered out, consider adjusting the percentile value.')
        self.data = self.data[flags == False]


    # ----- below are helper methods
    def _is_valid_for_analysis(self, data, test):
        """ Check whether the quality of data is good enough to perform analysis.
        Invalid cases can be 1. there is no data
                             2. the data does not contain all the variants to perform analysis
        :type data: DataFrame
        :type test: StatisticalTest
        :returns: boolean 
        """
        if data is None:
            logger.warning("Data is empty for the current analysis.")
            return False
        if len(data[data[test.variants.variant_column_name] == test.variants.control_name]) <= 1:
            logger.warning("Control group only contains 1 or 0 entities.")
            return False
        if len(data[data[test.variants.variant_column_name] == test.variants.treatment_name]) <= 1:
            logger.warning("Treatment group only contains 1 or 0 entities.")
        return True


    def _get_weights(self, data, kpi, variant):
        """
        Reweighting trick
        :param self: 
        :param data: 
        :param kpi: 
        :type  kpi: KPI
        :param variant: 
        :return: 
        """
        if type(kpi) is not DerivedKPI:
            return 1.0
        x = get_kpi_by_name_and_variant(data, kpi.reference_kpi, variant)
        number_of_zeros_and_nans      = sum(x == 0) + np.isnan(x).sum()
        number_of_non_zeros_and_nanas = len(x) - number_of_zeros_and_nans
        return number_of_non_zeros_and_nanas/np.nansum(x) * x


    def _quantile_filtering(self, kpis, percentile, threshold_type):
        # TODO: Add docstring
        method_table = {'upper': lambda x: x > threshold, 'lower': lambda x: x <= threshold}
        flags = pd.Series(data=[False]*len(self.data))
        for column in self.data[kpis].columns:
            threshold = np.percentile(self.data[column], percentile)
            flags = flags | self.data[column].apply(method_table[threshold_type])
        return flags
