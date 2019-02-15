'''
Module docstring
'''




import tempfile
import numpy as np

import pandas as pd

import pyomo.environ as po
from pyomo.core.base.objective import SimpleObjective

from grimsel import _get_logger

logger = _get_logger(__name__)


#def create_tempfile(self, suffix=None, prefix=None, text=False, dir=None):
#    """
#    Return the absolute path of a temporary filename that is
#    guaranteed to be unique.  This function generates the file and returns
#    the filename.
#    """
#
#    print('''
#          create_tempfile is monkey patched
#          ''')
#
#    if suffix is None:
#        suffix = ''
#    if prefix is None:
#        prefix = 'tmp'
#
#    ans = tempfile.mkstemp(suffix=suffix, prefix=prefix, text=text, dir=dir)
#    ans = list(ans)
#    if not os.path.isabs(ans[1]):  #pragma:nocover
#        fname = os.path.join(dir, ans[1])
#    else:
#        fname = ans[1]
#    os.close(ans[0])
#
#    dir = tempfile.gettempdir()
#
#    new_fname = os.path.join(dir, 'grimsel', 'grimsel_temp_'
#                             + ''.join(np.random.choice(list('abcdefghi'), 4))
#                             + suffix)
#    # Delete any file having the sequential name and then
#    # rename
#    if os.path.exists(new_fname):
#        os.remove(new_fname)
#    fname = new_fname
#
#    self._tempfiles[-1].append(fname)
#    return fname
#
#import pyutilib.component.config.tempfiles as tempfiles
#tempfiles.TempfileManagerPlugin.create_tempfile = create_tempfile


import sys
import os
from importlib import reload

from pyomo.opt import SolverFactory

# auxiliary modules
import grimsel.auxiliary.maps as maps
import grimsel.auxiliary.timemap as timemap

# model components
import grimsel.core.constraints as constraints
import grimsel.core.variables as variables
import grimsel.core.parameters as parameters
import grimsel.core.sets as sets
import grimsel.core.io as io # for class methods

TEMP_DIR = tempfile.gettempdir()

reload(constraints)
reload(variables)
reload(parameters)
reload(sets)

class ModelBase(po.ConcreteModel, constraints.Constraints,
                parameters.Parameters, variables.Variables, sets.Sets):

    # class attributes as defaults for presolve_fixed_capacities
    list_vars = [('var_yr_cap_pwr_rem', 'cap_pwr_rem'),
                 ('var_yr_cap_pwr_new', 'cap_pwr_new')]
    list_constr_deact = ['set_win_sol']

#    db = get_config('sql_connect')['db']


    def __init__(self, **kwargs):
        '''
        Keyword arguments:
        nhours -- time resolution of the model, used for profile scaling
        sc_warmstart -- input database schema for presolving
        slct_node -- limit node selection
        slct_encar -- limit to energy carrier selection
        skip_runs -- boolean; if True, solver calls are skipped, also
                     stops the IO instance from trying to write the model
                     variables.
        '''

        super(ModelBase, self).__init__() # init of po.ConcreteModel

        defaults = {'slct_node': [],
                    'slct_pp_type': [],
                    'slct_encar': ['EL'],
                    'nhours': 48,
                    'unq_code': '',
                    'mps': None,
                    'tm_filt': False,
                    'verbose_solver': True,
                    'constraint_groups': None,
                    'skip_runs': False,
                    'nthreads': False}
        for key, val in defaults.items():
            setattr(self, key, val)
        self.__dict__.update(kwargs)

        self._check_contraint_groups()

        logger.info('self.slct_encar=' + str(self.slct_encar))
        logger.info('self.slct_pp_type=' + str(self.slct_pp_type))
        logger.info('self.slct_node=' + str(self.slct_node))
        logger.info('self.nhours=' + str(self.nhours))
        logger.info('self.constraint_groups=' + str(self.constraint_groups))

        self.warmstartfile = self.solutionfile = None

        # attributes for presolve_fixed_capacities
        self.list_vars = ModelBase.list_vars
        self.list_constr_deact = ModelBase.list_constr_deact

    def build_model(self):
        '''
        Call the relevant model methods to get everything set up.

        This consists in:
        1. call self.get_setlst (in Sets mixin class) to initialize
            the self.setlst dictionary
        2. call self.define_sets (in Sets mixin class) to initialize
            Pyomo set objects
        3. call self.define_parameters (in Parameters mixin class)
        4. call self.define_variables (in Variables mixin class)
        5. call self.add_all_constraints
        6. call self.init_solver

        Note: io needs to have loaded all data, i.e. set the ModelBase
        DataFrames.
        '''

        self.get_setlst()
        self.define_sets()

        self.define_parameters()

        if not self.skip_runs:

            self.define_variables()
            self.add_all_constraints()
            self.init_solver()

    @classmethod
    def get_constraint_groups(cls, excl=None):
        '''
        Returns list names of methods defining constraint groups.

        This classmethod can also be used to define the constraint_groups
        parameter to initialize the ModelBase object by selecting certain
        groups to be excluded.

        Parameters:
        excl : exclude certain group names from the returned list
        '''

        cg_lst = [mth.replace('add_', '').replace('_rules', '')
                  for mth in dir(cls)
                  if mth.startswith('add_') and 'rule' in mth]

        if excl:
            cg_lst = [cg for cg in cg_lst if not cg in excl]

        return cg_lst


    def _check_contraint_groups(self):
        '''
        Verification and completion of the constraint group selection.

        Verifies constraint groups if the ``constraint_groups`` argument
        is not None. Otherwise it gathers all accordingly named
        methods from the class attributes and populates the list thusly.
        '''

        cg_options = self.get_constraint_groups()

        if self.constraint_groups is None:
            self.constraint_groups = cg_options
        else:
            # get invalid constraint groups in input
            nv = [cg for cg in self.constraint_groups
                  if not cg in cg_options]

            if nv:
                estr = ('Invalid constraint group(s): {nv}.'
                        + '\nPossible choices are:\n{cg}'
                        ).format(nv=', '.join(nv), cg=',\n'.join(cg_options))
                raise ValueError(estr)

    def add_all_constraints(self):
        '''
        Call all selected methods from the constraint mixin class.

        Loops through the `constraint_groups` list and calls the corresponding
        methods in the :class:`.Constraints` mixing class.
        '''

        for cg in set(self.constraint_groups):
            getattr(self, 'add_%s_rules'%cg)()


    def _limit_prof_to_cap(self):

        if len(self.chp) > 0:
            self.limit_prof_to_cap()


    def limit_prof_to_cap(self, param_mod='cap_pwr_leg'):
        '''
        Make sure CHP profiles don't ask for more power than feasible.

        This operates on the parameters and is called before each model run.
        '''

        print('*'*60 + '\nModelBase: Limiting chp profiles to cap_pwr_leg', end='... ')

        # get list of plants relevant for chp from corresponding set
        pp_chp = self.setlst['chp']

        df_chpprof = io.IO.param_to_df(self.chpprof, ('sy', 'nd_id', 'ca_id'))
        df_erg_chp = io.IO.param_to_df(self.erg_chp, ('pp_id', 'ca_id'))
        df_erg_chp = df_erg_chp.loc[df_erg_chp.pp_id.isin(pp_chp)]
        df_erg_chp['nd_id'] = df_erg_chp.pp_id.replace(self.mps.dict_plant_2_node_id)

        # outer join profiles and energy to get a profile for each fuel
        df_chpprof_tot = pd.merge(df_erg_chp.rename(columns={'value': 'erg'}),
                                  df_chpprof.rename(columns={'value': 'prof'}),
                                  on=['nd_id', 'ca_id'])
        # scale profiles
        df_chpprof_tot['prof_sc'] = df_chpprof_tot['erg'] * df_chpprof_tot['prof']

        # get capacities from parameter
        df_cap_pwr_leg = io.IO.param_to_df(self.cap_pwr_leg, ('pp_id', 'ca_id'))
        # keep only chp-related fuels
        df_cap_pwr_leg = df_cap_pwr_leg.loc[df_cap_pwr_leg.pp_id.isin(self.chp)]
        # pivot_by fl_id
        df_cappv = df_cap_pwr_leg.pivot_table(values='value',
                                              index=['ca_id', 'pp_id'],
                                              aggfunc=np.sum)['value']
        # rename
        df_cappv = df_cappv.rename('cap').reset_index()

        # add capacity to profiles
        df_chpprof_tot = pd.merge(df_cappv, df_chpprof_tot, on=['ca_id', 'pp_id'])


        # find occurrences of capacity zero and chp erg non-zero
        df_slct = df_chpprof_tot[['pp_id', 'ca_id', 'cap', 'erg']].drop_duplicates().copy()
        df_slct = df_slct.loc[df_slct.cap.isin([0])
                            & -df_slct.erg.isin([0])]
        str_erg_cap = ''
        if len(df_slct > 0):
            for nrow, row in df_slct.iterrows():
                str_erg_cap += 'pp_id=%d, ca_id=%d: cap_pwr_leg=%f, erg_chp=%f\n'%tuple(row.values)
            raise ValueError ('limit_prof_to_cap: one or more cap_pwr_leg are zero '
                              'while erg_chp is greater 0: \n' + str_erg_cap)

        # find occurrences of capacity violations
        mask_viol = df_chpprof_tot.prof_sc > df_chpprof_tot.cap


        if mask_viol.sum() == 0:
            print('ok, nothing changed.\n' + '*'*60)
        else:
            # REPORTING
            df_profviol = df_chpprof_tot.loc[mask_viol]
            dict_viol = df_profviol.pivot_table(index=['pp_id', 'ca_id'],
                                                values='sy', aggfunc=len)['sy'].to_dict()

            for kk, vv in dict_viol.items():
                print('\n(pp, ca)={}: {} violations'.format(kk, vv))

            print('Modifing model parameter ' + param_mod, end=' ... ')

            if param_mod == 'chpprof':

                df_profviol['prof'] *=  0.999 * df_chpprof_tot.cap / df_chpprof_tot.prof_sc
                dict_chpprof = (df_profviol.pivot_table(index=['sy', 'nd_id', 'ca_id'],
                                                        values='prof', aggfunc=min)['prof']
                                           .to_dict())

                for kk, vv in dict_chpprof.items():
                    self.chpprof[kk] = vv

            elif param_mod == 'cap_pwr_leg':

                # calculate capacity scaling factor
                df_capsc = df_profviol.pivot_table(index=['pp_id', 'ca_id'],
                                                   values=['cap', 'prof_sc'], aggfunc=np.max)
                df_capsc['cap_sc'] = df_capsc.prof_sc / df_capsc.cap

                # merge scaling factor with capacity table
                df_cap_pwr_leg = df_cap_pwr_leg.join(df_capsc,
                                                     on=df_capsc.index.names)
                df_cap_pwr_leg = df_cap_pwr_leg.loc[-df_cap_pwr_leg.cap_sc.isnull()]

                # apply scaling factor to all capacity with the relevant fuel
                df_cap_pwr_leg['cap'] *= df_cap_pwr_leg.cap_sc * 1.0001

                # dictionary
                dict_cap_pwr_leg = df_cap_pwr_leg.set_index(['pp_id', 'ca_id'])['cap']
                dict_cap_pwr_leg = dict_cap_pwr_leg.to_dict()

                for kk, vv in dict_cap_pwr_leg.items():
                    self.cap_pwr_leg[kk] = vv

            print('done.\n' + '*'*60)

    def _init_pf_dicts(self):
        '''
        Initializes dicts mapping the profile ids to other model ids.

        This results in dictionaries which are assigned as :class:`ModelBase`
        instance attributes:

        * ``dict_pricesll_pf``: (fl_id, nd_id, ca_id) |rarr| (pricesll_pf_id)
        * ``dict_pricebuy_pf``: (fl_id, nd_id, ca_id) |rarr| (pricebuy_pf_id)
        * ``dict_dmnd_pf``: (nd_id, ca_id) |rarr| (dmnd_pf_id)
        * ``dict_supply_pf``: (pp_id, ca_id) |rarr| (supply_pf_id)

        Use:
        ____

        The resulting dictionaries are used for filtering the profile tables
        in the :module:`io` module and to access the profile parameters
        in the model :class:`Constraints`.

        '''

        list_pf = [(self.df_fuel_node_encar,
                    ['fl_id', 'nd_id', 'ca_id'], 'pricebuy'),
                   (self.df_fuel_node_encar,
                    ['fl_id', 'nd_id', 'ca_id'], 'pricesll'),
                   (self.df_node_encar,
                    ['nd_id', 'ca_id'], 'dmnd'),
                   (self.df_plant_encar,
                    ['pp_id', 'ca_id'], 'supply')]

        df, ind, name = list_pf[-1]
        for df, ind, name in list_pf:

            col = '%s_pf_id'%name
            ind_df = df.loc[~df[col].isna()].set_index(ind)[col]

            dct = ind_df.to_dict()

            setattr(self, 'dict_%s_pf'%name, dct)

    def translate_pf_id(self, df):
        '''
        Adds model id columns for the profile ids in the input DataFrame.

        Searches vars(self) for the pf_dict corresponding to the pf_ids
        in the input DataFrame. Then uses this dictionary to add additional
        columns to the output table.

        Parameters
        ----------
        df (DataFrame): DataFrame with pf_id column.

        Returns
        -------
        :obj:`pandas.DataFrame`
            Input DataFrame with added model ids corresponding to the pf_id.

        Raises
        ------
        IndexError: If multiple pf dictionaries correspond to the pf_id
                    values in the input DataFrame.
        IndexError: If no pf dictionary can be found for the pf_id values.

        '''

        # identify corresponding pf dict
        list_pf_id = set(df.pf_id.unique().tolist())

        pf_arrs = {name_dict:
                    list_pf_id
                        .issubset(set(getattr(self, name_dict).values()))
                    for name_dict in vars(self)
                    if name_dict.startswith('dict_')
                    and name_dict.endswith('_pf')}

        if sum(pf_arrs.values()) > 1:
            raise IndexError('Ambiguous pf array in translate_pf_id.')
        elif sum(pf_arrs.values()) == 0:
            raise IndexError('No pf array found for table with columns '
                             '%s'%df.columns.tolist())
        else:
            pf_dict = {val: key for key, val in pf_arrs.items()}[True]

            new_cols = {'dict_pricesll_pf': ['fl_id', 'nd_id', 'ca_id'],
                        'dict_pricebuy_pf': ['fl_id', 'nd_id', 'ca_id'],
                        'dict_price_pf': ['fl_id', 'nd_id', 'ca_id'],
                        'dict_dmnd_pf': ['nd_id', 'ca_id'],
                        'dict_supply_pf': ['pp_id', 'ca_id']}[pf_dict]

            pf_dict = getattr(self, pf_dict)


            df_new = pd.Series(pf_dict).reset_index()
            df_new.columns = new_cols + ['pf_id']

            df_new = pd.merge(df_new, df, on='pf_id')

            return df_new


    def _get_nhours_nodes(self, nhours):
        '''
        Generates the nhours dictionary ``nhours``.

        Returns
        -------
            nhours_dict (dict): ``{node: (original time res,
                                          target time res)}``

        '''


        if isinstance(nhours, dict):

            nhours_dict = {}

            for nd in self.slct_node:

                nd_id = self.mps.dict_nd_id[nd]

                if nd in nhours:

                    if isinstance(nhours[nd], tuple):
                        # all there
                        nhours_dict[nd_id] = nhours[nd]

                    elif isinstance(nhours[nd], (float, int)):
                        # assuming original time resolution 1 hour
                        nhours_dict[nd_id] = (1, nhours[nd])

                else:
                    # assuming default
                    nhours_dict[nd_id] = (1, 1)

        elif isinstance(nhours, (float, int)):

            nhours_dict = {nd: (1, nhours) for nd in self.slct_node_id}

        return nhours_dict


    def init_maps(self):
        '''
        Uses the input DataFrames to initialize a :class:Maps instance.

        '''

        dct = {var.replace('df_def_', ''): getattr(self, var)
               for var in vars(self) if 'df_def_' in var}

        self.mps = maps.Maps.from_dicts(dct)

    def _init_time_map_connect(self):

        df_ndcnn = self.df_node_connect[['nd_id', 'nd_2_id', 'ca_id']].drop_duplicates()

        df_ndcnn['freq'] = df_ndcnn.nd_id.apply(lambda x: {key: frnh[0] for key, frnh in self.dict_nd_tm.items()}[x])
        df_ndcnn['nhours'] = df_ndcnn.nd_id.apply(lambda x: {key: frnh[1] for key, frnh in self.dict_nd_tm.items()}[x])
        df_ndcnn['freq_2'] = df_ndcnn.nd_2_id.apply(lambda x: {key: frnh[0] for key, frnh in self.dict_nd_tm.items()}[x])
        df_ndcnn['nhours_2'] = df_ndcnn.nd_2_id.apply(lambda x: {key: frnh[1] for key, frnh in self.dict_nd_tm.items()}[x])
        df_ndcnn['tm_id'] = df_ndcnn.nd_id.replace(self.dict_nd_tm_id)
        df_ndcnn['tm_2_id'] = df_ndcnn.nd_2_id.replace(self.dict_nd_tm_id)

        # make dict_sy_ndnd_min
        is_min_node = pd.concat([df_ndcnn,
                                 df_ndcnn.assign(nd_id = df_ndcnn.nd_2_id,
                                                 nd_2_id = df_ndcnn.nd_id,
                                                 nhours = df_ndcnn.nhours_2,
                                                 nhours_2 = df_ndcnn.nhours)])
        self.is_min_node = (
                is_min_node.assign(is_min=is_min_node.nhours
                                   <= is_min_node.nhours_2)
                                   .set_index(['nd_id', 'nd_2_id'])
                                   .is_min).to_dict()

        def get_map_sy(x):
            tm = timemap.TimeMap(tm_filt=self.tm_filt, minimum=True,
                                 freq=x[['freq', 'freq_2']].min(axis=1).values[0],
                                 nhours=x.nhours.iloc[0])
            tm_2 = timemap.TimeMap(tm_filt=self.tm_filt, minimum=True,
                                   freq=x[['freq', 'freq_2']].min(axis=1).values[0],
                                   nhours=x.nhours_2.iloc[0])
            return pd.merge(tm.df_hoy_soy,
                            tm_2.df_hoy_soy.rename(columns={'sy': 'sy2'}),
                            on='hy')[['sy', 'sy2']]

        self.dict_ndnd_tm_id =  df_ndcnn.set_index(['nd_id', 'nd_2_id']).copy()
        self.dict_ndnd_tm_id['tm_min_id'] = self.dict_ndnd_tm_id.apply(lambda x: x.tm_id if x.nhours <= x.nhours_2 else x.tm_2_id, axis=1)
        self.dict_ndnd_tm_id = self.dict_ndnd_tm_id.tm_min_id.to_dict()
        self.dict_ndnd_tm_id = {**self.dict_ndnd_tm_id,
                                **{(key[1], key[0]): val
                                   for key, val
                                   in self.dict_ndnd_tm_id.items()}}

        sysymap = df_ndcnn[[c for c in df_ndcnn.columns if not 'nd' in c]]
        sysymap = df_ndcnn.drop_duplicates()
        sysymap = df_ndcnn.groupby(['tm_id', 'tm_2_id']).apply(get_map_sy).reset_index()
        sysymap = sysymap.drop('level_2', axis=1)

        self.df_sysy_ndcnn = pd.merge(
                df_ndcnn[['nd_id', 'nd_2_id', 'ca_id', 'tm_id', 'tm_2_id']],
                sysymap, on=['tm_id', 'tm_2_id'], how='outer')

        self.dict_sysy = {**self.df_sysy_ndcnn.groupby(['nd_id', 'nd_2_id', 'sy']).sy2.apply(lambda x: set((*x,))).to_dict(),
                          **self.df_sysy_ndcnn.groupby(['nd_2_id', 'nd_id', 'sy2']).sy.apply(lambda x: set((*x,))).to_dict()}

        self.df_symin_ndcnn = self.df_sysy_ndcnn.join(df_ndcnn.set_index(['nd_id', 'nd_2_id'])[['nhours', 'nhours_2']], on=['nd_id', 'nd_2_id'])

        idx = ['nd_id', 'nd_2_id']
        cols = ['sy', 'sy2', 'tm_2_id', 'ca_id', 'tm_id', 'nhours', 'nhours_2']
        list_df = []
        for nd_id, nd_2_id in set(self.df_symin_ndcnn.set_index(idx).index.values):
            df = self.df_symin_ndcnn.set_index(idx).loc[(nd_id, nd_2_id), cols]

            nd_smaller = df.nhours.iloc[0] <= df.nhours_2.iloc[0]
            list_df.append(df.assign(symin = df.sy if nd_smaller else df.sy2,
                                     tm_min_id = df.tm_id if nd_smaller
                                                 else df.tm_2_id))

        cols = ['tm_min_id', 'symin', 'nd_id', 'nd_2_id', 'ca_id']
        self.df_symin_ndcnn = pd.concat(list_df).reset_index()[cols]


    def _init_time_map(self):
        '''
        Create a TimeMap instance and obtain derived attributes.

        Generated attributes:
            * ``tm`` (``TimeMap``): TimeMap object
            * ``df_tm_soy_full`` (``DataFrame``): full timemap table
            * ``df_tm_soy_full`` (``DataFrame``): full timemap table
        '''


        self.dict_nd_tm = self._get_nhours_nodes(self.nhours)

        self.dict_tm = {ntm: frnh for ntm, frnh
                        in enumerate(set(self.dict_nd_tm.values()))}

        self.dict_nd_tm_id = {nd: {val: key for key, val
                                   in self.dict_tm.items()}[tm]
                              for nd, tm in self.dict_nd_tm.items()}

        self.tm_objs = {tm_id: timemap.TimeMap(tm_filt=self.tm_filt,
                                       nhours=frnh[1], freq=frnh[0])
                        for tm_id, frnh in self.dict_tm.items()}

        self.df_def_node['tm_id'] = (self.df_def_node.reset_index().nd_id
                                               .replace(self.dict_nd_tm_id))

        cols_red = ['wk_id', 'mt_id', 'sy', 'weight', 'wk_weight']
        list_tm_soy = [tm.df_time_red[cols_red].assign(tm_id=tm_id) for
                       tm_id, tm in self.tm_objs.items()]

        self.df_tm_soy = pd.concat(list_tm_soy, axis=0)
        list_tm_soy_full = [tm.df_time_red.assign(tm_id=tm_id)
                            for tm_id, tm in self.tm_objs.items()]
        self.df_tm_soy_full = pd.concat(list_tm_soy_full, axis=0)
        list_hoy_soy = [tm.df_hoy_soy.assign(tm_id=tm_id)
                        for tm_id, tm in self.tm_objs.items()]
        self.df_hoy_soy = pd.concat(list_hoy_soy, axis=0)

        _df = self.df_tm_soy

        # get dictionaries month/week <-> time slots;
        # these are used in the constraint definitions
        cl, nm = ('wk_id', 'week')
        for cl, nm in [('wk_id', 'week'), ('mt_id', 'month')]:

            dct = _df.pivot_table(index=['tm_id', cl],
                                  values='sy', aggfunc=list).sy.to_dict()
            setattr(self, 'dict_' + nm + '_soy', dct)

            dct = _df.set_index(['tm_id', 'sy'])[cl].to_dict()
            setattr(self, 'dict_soy_' + nm, dct)


        # dict pp_id -> tm
        self.dict_pp_tm_id = (
            self.df_def_plant.assign(tm_id=self.df_def_plant.nd_id
                                               .replace(self.dict_nd_tm_id))
                             .set_index('pp_id').tm_id.to_dict())


        # dict tm_id -> sy
        unq_list = lambda x: list(set(x))
        pv_kws = dict(index='tm_id', values='sy', aggfunc=unq_list)
        self.dict_tm_sy = self.df_hoy_soy.pivot_table(**pv_kws).sy.to_dict()


    def _soy_map_hydro_bcs(self):
        ''' Map hydro boundary conditions (which refer to the beginning
            of the month) to time slots
        '''

        if not self.df_plant_month is None:
            _df = self.df_def_month[['mt_id', 'month_min_hoy']]
            _df = _df.set_index('mt_id')
            self.df_plant_month = self.df_plant_month.join(_df,
                                                           on=_df.index.name)
            self.df_plant_month['tm_id'] = (
                    self.df_plant_month.pp_id
                        .replace(self.mps.dict_plant_2_node_id)
                        .replace(self.dict_nd_tm_id))
            _df = self.df_hoy_soy.rename(columns={'hy': 'month_min_hoy'})
            _df = _df.set_index(['month_min_hoy', 'tm_id'])
            self.df_plant_month = self.df_plant_month.join(_df,
                                                           on=_df.index.names)

            sy_null = self.df_plant_month.sy.isnull()
            self.df_plant_month = self.df_plant_month.loc[-sy_null]


    def map_to_time_res(self):
        '''
        Generates a map between hours-of-the-year and time slots-of-the-year
        based on the fixed time resolution self.nhours, using the class
        timemap.TimeMap. Then maps relevant input data from hours to slots.
        Also generates dictionaries which contain all slot ids for each
        week/month and vice versa.

        Raises:
            ValueError: If multiple tm_ids correspond to the pf_ids in any
                selected profile tables. This could be fixed by splitting up
                the profile table, but we rather have this avoided on the
                input data side.
        '''

        self._init_time_map()
        self._init_time_map_connect()

        print('+++++++++++++++++++++++++ SKIPPING adjust_cost_time +++++++++++++++++++++++++++')
#        self.adjust_cost_time()
        self._soy_map_hydro_bcs()


        # Map profiles and bc to soy
        for itb, idx in [
                         ('dmnd', ['dmnd_pf_id', 'sy']),
                         ('inflow', ['pp_id', 'ca_id', 'sy']),
                         ('supply', ['supply_pf_id', 'sy']),
                         ('chp', ['nd_id', 'ca_id', 'sy']),
                         ('pricesll', ['price_pf_id', 'sy']),
                         ('pricebuy', ['price_pf_id', 'sy']),
                        ]:

            name_df = 'df_prof' + itb
            print('Averaging {}; nhours={}.'.format(name_df, self.nhours))

            df_tbsoy = getattr(self, name_df)

            self._map_profile_to_time_resolution(df=df_tbsoy, itb=itb, idx=idx)


    def _add_tm_columns(self, df):
        '''
        Adds a ``tm_id`` column to ``df`` based on suitable other indices.

        Depending on availability, the profile (``pf_id``), plant(``pp_id``)
        or node (``nd_id``) column is used to obtain a timemap (``tm_id``)
        column.

        Parameters
        ----------
        df: DataFrame
            Input table

        Returns
        -------
        DataFrame
            table with additional ``tm_id`` column,

        Raises
        ------
        IndexError
            When ``df`` has neither a ``nd_id`` column nor a ``pp_id`` column
            nor a ``pf_id`` column.
        '''

        cols = df.columns.tolist()

        if not any(col_slct in cols
                   for col_slct in ['pp_id', 'nd_id']):


            list_pf_col = [c for c in cols if 'pf_id' in c]
            if list_pf_col:
                pf_id = df[list_pf_col[0]]
                df = self.translate_pf_id(df.assign(pf_id=pf_id))
            else:
                raise IndexError('_add_tm_columns: '
                                 'no nd_id, pf_id, or pp_id column '
                                 'in table with columns%s.'%cols)

        if 'pp_id' in df.columns:

            dct_p2tm = self.dict_pp_tm_id
            df['tm_id'] = df.pp_id.replace(dct_p2tm)

        elif 'nd_id' in df.columns:

            dct_n2tm = self.dict_nd_tm_id
            df['tm_id'] = df.nd_id.replace(dct_n2tm)

        return df[cols + ['tm_id']]


    def _map_profile_to_time_resolution(self, df, itb, idx):
        ''' Maps a single profile table to the selected nodal time resolution.

        Parameters
        ----------
        df: DataFrame
            input profile table. Must have columns ``hy`` as well as one of
            ``{tm_id, pp_id, nd_id, pf_id}``
        itb: str
            table name, used for ``setattr`` of new tables
        idx: list of str
            index columns of the old and new tables

        Returns
        -------
        None
            Mapped table is made an attribute of the :class:`ModelBase` class
            instance

        Raises
        ------
        IndexError
            When a given ``pf_id`` is used by multiple ``tm_id``. E.g. if
            a price profile is used by different nodes with different
            time resolutions. Could be implemented... not done for now.

        '''

        df = df.copy()

        val = ['value']

        if df is None or df.empty:
            setattr(self, 'df_prof' + itb + '_soy',
                    pd.DataFrame(columns=idx + val))
            return None

        if not 'tm_id' in df.columns:
            df = self._add_tm_columns(df)

        # are there single nodes pf_ids which have more than one tm_id?
        if 'pf_id' in df.columns:
            df_pf = df[['pf_id', 'tm_id']].drop_duplicates()
            tm_count = df_pf.groupby('pf_id').tm_id.count()
            if tm_count.max() > 1:
                raise IndexError('map_to_time_res: Multiple tm_ids '
                                 'found for pf_ids of %s profiles.'%itb)

        df['hy'] = df.hy.astype(float)

        ind = ['hy', 'tm_id']
        df = df.join(self.df_hoy_soy.set_index(ind), on=ind)

        df[val] = df[val].astype(float)
        df = df.pivot_table(values=val, index=idx,
                            aggfunc=np.mean).reset_index()

        if df.empty:
            df = pd.DataFrame(columns=idx + val)

        setattr(self, 'df_prof' + itb + '_soy', df)

    def adjust_cost_time(self):
        '''
        Scale fixed costs for incomplete years.

        If the model year doesn't have 8760 hours, fixed costs are scaled
        to keep levelized costs the same.

        This is relevant if the tm_filt ModelBase parameter is used to
        work with a simplified model version.
        '''
        tm_filt_weight = self.tm.tm_filt_weight

        lstfc = [c for c in self.df_plant_encar.columns if c.startswith('fc_')]

        self.df_plant_encar[lstfc] /= tm_filt_weight

    def get_maximum_demand(self):
        '''
        Calculation of maximum demand (in MW) with adjusted time resolution.

        Note: Database table def_node is updated in the
        :func:`io.write_runtime_tables` method.

        '''

        if (hasattr(self, 'df_profdmnd_soy')
            and not self.df_profdmnd_soy.empty):
            df = self.df_profdmnd_soy
            df_dmd_params = df.pivot_table(values=['value'],
                                           index='dmnd_pf_id',
                                           aggfunc=[max]).reset_index()
            df_dmd_params.columns = ['pf_id', 'dmnd_max']
            df_dmd_params = self.translate_pf_id(df_dmd_params)
            df_dmd_params = df_dmd_params.set_index('nd_id')[['dmnd_max']]

            self.df_def_node.drop([c for c in self.df_def_node.columns
                                   if c in df_dmd_params.columns],
                                  axis=1, inplace=True)

            self.df_def_node = self.df_def_node.join(df_dmd_params, on='nd_id')

    def switch_soln_file(self, isolnfile):
        '''
        Warmstart and solutionfiles are alternated between two to
        allow to use the last solution as starting values but avoid
        excessive disk space use (which would be the case if we kept all
        solution files).

        Parameters:
        isolnfile -- binary index of last solution file
        '''
        isolnfile = 1 - isolnfile
        solnfile = os.path.join(TEMP_DIR,
                                ('manual_soln_file_{uc}_{i}.cplex.sol'
                                 .format(uc=self.unq_code, i=str(isolnfile))))
        return solnfile, isolnfile

    def init_solver(self):
        '''
        Create pyomo Solverfactory instance and adjust parameters.



        '''
        self.dual = po.Suffix(direction=po.Suffix.IMPORT)

        if sys.platform == 'win32':
            self.solver = SolverFactory("cplex")
        elif sys.platform in ['linux2', 'linux']:
            exec_str = ('/opt/ibm/ILOG/CPLEX_Studio1271/cplex/bin/'
                        +'x86-64_linux/cplex')
            self.solver = SolverFactory("cplex", executable=exec_str)
        elif sys.platform == 'darwin':
            exec_str = ('/Applications/CPLEX_Studio128/cplex/bin/'
                        'x86-64_osx/cplex')
            self.solver = SolverFactory("cplex", executable=exec_str)

        if self.nthreads:
            self.solver.set_options('threads=' + str(self.nthreads))

        fn = 'manual_log_file_{uc}.cplex.sol'.format(uc=self.unq_code)
        self.logfile = os.path.join(TEMP_DIR, fn)
        self.solver._problem_files = (os.path.join(TEMP_DIR, 'pyomo.lp'),)

        # init of solutionfile
        self.isolnfile = 0
        self.solutionfile, self.isolnfile = self.switch_soln_file(1)
        self.warmstartfile = None

    def check_valid_indices(self, index, log_str=''):
        '''
        Used in parameter and variable definitions.

        Parameters:
        index: tuple of pyomo sets
        '''

        is_empty = [pi.name for pi in index if (not pi is None) and not pi]
        is_none = [pi is None for pi in index]
        if any(is_empty) + any(is_none):
            print((log_str + 'failed: set(s) {} is/are '
                   + 'empty or None.').format(str(is_empty)))
            return False
        else:
            return True

    def delete_component(self, comp_name):
        '''
        Drop a component of the pyomo model.

        A single component object is associated with various index objects.
        Because of this, some looping over the vars is required
        to catch 'em all.

        Parameters
        ----------
        comp_name (str): base name of the model component (variable, etc)

        '''

        list_del = [vr for vr in vars(self)
                    if comp_name == vr
                    or vr.startswith(comp_name + '_index')
                    or vr.startswith(comp_name + '_domain')]

        list_del_str = ', '.join(list_del)
        logger.info('Deleting model components ({}).'.format(list_del_str))

        for kk in list_del:
            self.del_component(kk)

    def run(self, warmstart=False):
        '''
        Run the model. Then switch solution/warmstartfile.

        Unless skip_runs is True. Then just create a pro-forma results object.

        Args:
            warmstart (bool): passed to the Solver solve call
        '''

        if self.skip_runs:
            class Result: pass # ad-hoc class mimicking the results object
            self.results = Result()
            self.results.Solver = [{'Termination condition':
                                    'Skipped due to skip_runs=True.'}]
        else:
            self.results = self.solver.solve(self, tee=self.verbose_solver,
                                             keepfiles=True,
                                             warmstart=warmstart,
                                             solnfile=self.solutionfile,
                                             logfile=self.logfile,
                                             warmstart_file=self.warmstartfile)
            self.warmstartfile = self.solutionfile
            sf, isf = self.switch_soln_file(self.isolnfile)
            self.solutionfile, self.isolnfile = [sf, isf]

            self._get_objective_value()

    def _get_objective_value(self):
        '''
        Makes the objective value a :class:`ModelBase` instance attribute.

        This assumes that among the objects defined by ``list_obj_name``
        only one actually exists.
        '''

        list_name_obj = ['objective_lin', 'objective_quad', 'objective']


        if self.results.solver.termination_condition.key == 'optimal':
            for name_obj in list_name_obj:
                obj = getattr(self, name_obj, False)
                if obj and isinstance(obj, SimpleObjective) and obj.active:
                    self.objective_value = po.value(obj)
        else:
            self.objective_value = np.nan


    def print_is_fixed(self, variable='cap_pwr_new'):
        '''
        Print all fixed elements of the variable given as an input.
        TODO: This assumes pp as first index: needs to change!
        Keyword arguments:
        variable -- name string of a pyomo variable
        '''
        vv = getattr(self, variable)
        print('*'*15 + ' ' + variable + ': ' + '*'*15)
        for i in vv:
            print('vv:', vv)
            if vv[i].fixed:
                print(str(self.mps.dict_pp[i[0]]) + ': is fixed at '
                      + str(vv[i].value))
        print('*'*(33 + len(variable)) + '\n')

    def fill_peaker_plants(self, demand_factor=1.02, reset_to_zero=False,
                           list_peak=[]):
        '''
        Calculate required capacity of designated peaker plants from
        power capacity and demand profiles.
        This serves to avoid infeasibilities due to insufficient installed
        capacity in case the capacities are fixed.
        Keyword arguments:
        demand_factor -- multiplied with the peak load value, this determines
                         the required dispatchable power capacity
        from_variable -- boolean, if True, use variable 'cap_pwr_tot' instead
                         of default 'cap_pwr_leg'
        reset_to_zero -- boolean, if True: stop after the reset-to-zero stage

        '''

        list_attr = ['cap_pwr_leg']

        # reset to zero: get list of peaker plants ppca indices from
        # df_plant_encar then set both cap_pwr_tot and cap_pwr_leg to zero
        if not list_peak:
            _df = self.df_plant_encar
            _list_peak = _df.loc[_df['pp_id'].isin(self.setlst['peak']),
                                  ['pp_id', 'ca_id']].drop_duplicates()
            _list_peak = _list_peak.apply(tuple, axis=1).tolist()
        else:
            _list_peak = list_peak

        for iattr in list_attr:
            for kk in list_peak:
                getattr(self, iattr)[kk] = 0

        if (self.setlst['peak'] or list_peak) and not reset_to_zero:

            slct_attr = 'cap_pwr_leg'

            # Ids all plants considered dispatchable, not peaker plants.
            slct_pp = [pp for pp in
                       self.setlst['pp']
                       + self.setlst['st']
                       + self.setlst['hyrs']
                       if not pp in self.setlst['peak']]

            list_cap = [tuple(list(cc)
                              + [self.mps.dict_plant_2_node_id[cc[0]]]
                              + [getattr(self, slct_attr)[cc].value])
                        for cc in getattr(self, slct_attr) if cc[0] in slct_pp]

            # generate df with all capacities of dispatchable plants
            _df = pd.DataFrame(list_cap, columns=['pp_id', 'ca_id',
                                                  'nd_id', 'cap_pwr'])

            df_dmd_max = self.df_def_node.set_index('nd_id')['dmnd_max']
            df_dmd_max *= demand_factor

            df_cap_tot = pd.DataFrame(_df.pivot_table(values=['cap_pwr'],
                                          index='nd_id', aggfunc=sum),
                                          columns=['cap_pwr'],
                                          index=df_dmd_max.index).fillna(0)
            df_cap_tot = df_cap_tot['cap_pwr']

            dict_nd_pp = {nd: pp for pp, nd in
                          self.mps.dict_plant_2_node_id.items()
                          if pp in list(zip(*_list_peak))[0]}

            df_cap_peak = df_dmd_max - df_cap_tot
            df_cap_peak = df_cap_peak.apply(lambda x: max(0., x))
            df_cap_peak = df_cap_peak.reset_index()
            df_cap_peak['pp_id'] = df_cap_peak['nd_id']
            list_nd = list(dict_nd_pp.keys())
            df_cap_peak = df_cap_peak.loc[df_cap_peak.nd_id.isin(list_nd)]
            df_cap_peak['pp_id'] = df_cap_peak['pp_id'].replace(dict_nd_pp)
            df_cap_peak['ca_id'] = 0

            df_cap_peak = df_cap_peak.set_index(['pp_id', 'ca_id'])
            dict_peak_1 = df_cap_peak[0].to_dict()

            for iattr in list_attr:
                for kk, vv in dict_peak_1.items():
                    getattr(self, iattr)[kk] = vv


    def fix_scenario_plants(self):
        '''
        Make sure exogenously defined capacities cannot be optimized.
        '''
        for tc in self.setlst['scen']:
            self.cap_pwr_new[(tc, 0)].fix()
            self.cap_pwr_rem[(tc, 0)].fix()


    def do_zero_run(self):
        '''
        Perform a single model run without capacity retirements/additions
        '''

        # set price of co2 to 5
        for ipp in self.price_co2:
            self.price_co2[ipp] = 5

        # no retirements or investments of capacity
        self.capchnge_max = 0

        tstr = '** First run without investments and retirements **'
        print('*' * len(tstr) + '\n' + tstr + '\n' + '*' * len(tstr))

        self.run(warmstart=False)

        # retirements or investments parameter back to inf
        self.capchnge_max = float('Inf')

    def activation(self, bool_act=False, constraint_list=False,
                   subset=False, verbose=False):
        ''' Changes activation of a list of constraints to bool_act '''

        if subset:
            if type(subset) is not dict:
                _subset = {c: subset for c in constraint_list}
            else:
                _subset = subset
        else:
            _subset = {c: [ii for ii in getattr(self, c)]
                           for c in constraint_list}

        for iconst in constraint_list:
            obj_constr = getattr(self, iconst)

            for ii in _subset[iconst]:
                if bool_act:
                    getattr(self, iconst)[ii].activate()
                else:
                    getattr(self, iconst)[ii].deactivate()

            if verbose:
                if type(verbose) == bool:
                    verbose = len(obj_constr)
                print(obj_constr)
                for ikk, kk in enumerate(obj_constr):
                    if ikk <= verbose :
                        print('{}: {}; is active: {}'.format(kk, obj_constr[kk],
                                                            obj_constr[kk].active))
                print('...\n' if verbose < len(obj_constr) else 'end\n')


    def set_variable_const(self, value=0, variable_list=False, verbose=False):

        for varname in variable_list:
            obj_var = getattr(self, varname)
            keys_var = [c for c in obj_var]
            dict_new = {kk: value for kk in keys_var}
            obj_var.set_values(dict_new)

            if verbose:
                if type(verbose) == bool:
                    verbose = len(obj_var)
                print(varname)
                for ikk, kk in enumerate(obj_var):
                    if ikk <= verbose:
                        print('{}: {}; is fixed: {}'.format(kk, obj_var[kk].value,
                                                            obj_var[kk].fixed))
                print('...\n' if verbose < len(obj_var) else 'end\n')

    def set_variable_fixed(self, bool_fix=True, variable_list=False,
                           subset=False, exclude=False, verbose=False):


        for varname in variable_list:
            obj_var = getattr(self, varname)


            _exclude = exclude if exclude else []
            _subset = [c for c in obj_var if not c in _exclude] if not subset else subset



            for ii in _subset:
                if bool_fix:
                    obj_var[ii].fix()
                else:
                    obj_var[ii].unfix()

            if verbose:
                if type(verbose) == bool:
                    verbose = len(obj_var)
                print(varname)
                for ikk, kk in enumerate(obj_var):
                    if ikk <= verbose and kk in _subset:
                        print('{}: {}; is fixed: {}'.format(kk, obj_var[kk].value,
                                                            obj_var[kk].fixed))
                print('...\n' if verbose < len(obj_var) else 'end\n')



    def scale_nodes(self, nodes, comp_slct=None):
        '''
        Scale relevant components with a node-specific factor.

        Parameters
        ----------
        nodes: dict
            node: factor dictionary with types ``{str: numeric}``
        comp_slct: list of str
            Names of components whose values are to be scaled. Subset of
            ``['dmnd', 'cap_pwr_leg', 'erg_inp', 'erg_chp', 'cap_trme_leg',
               'cap_trmi_leg']``

        '''

        if not comp_slct:
            comp_slct = ['dmnd', 'cap_pwr_leg', 'erg_inp', 'erg_chp',
                         'cap_trme_leg', 'cap_trmi_leg']

        for nd, scale in nodes.items():

            nd_id = self.mps.dict_nd_id[nd]

            list_pp_id = self.df_def_plant.loc[self.df_def_plant.nd_id
                                               == nd_id].pp_id.tolist()

            mk_ndca = self.df_node_encar.nd_id == nd_id
            list_ndca_id = self.df_node_encar.loc[mk_ndca, ['nd_id', 'ca_id']]
            list_ndca_id = list_ndca_id.apply(tuple, axis=1).tolist()

            mk_ppca = self.df_plant_encar.pp_id.isin(list_pp_id)
            list_ppca_id = self.df_plant_encar.loc[mk_ppca, ['pp_id', 'ca_id']]
            list_ppca_id = list_ppca_id.apply(tuple, axis=1).tolist()

            if 'dmnd' in comp_slct:
                list_pf_id_dmnd = [self.dict_dmnd_pf[ndca]
                                   for ndca in list_ndca_id]
                for key in self.dmnd.sparse_keys():
                    if key[-1] in list_pf_id_dmnd:
                        self.dmnd[key] *= scale

            if 'cap_pwr_leg' in comp_slct:
                for key in self.cap_pwr_leg.sparse_keys():
                    if key in list_ppca_id:
                        self.cap_pwr_leg[key] *= scale

            if 'erg_inp' in comp_slct:
                for key in self.erg_inp.sparse_keys():
                    if key[0] == nd_id and self.erg_inp[key].value:
                        self.erg_inp[key] *=scale

            if 'erg_chp' in comp_slct:
                for key in self.erg_chp.sparse_keys():
                    if key in list_ppca_id and self.erg_chp[key].value:
                        self.erg_chp[key] *=scale

            if 'cap_trme_leg' in comp_slct:
                for key in self.cap_trme_leg.sparse_keys():
                    if nd_id in key[1:3]:
                        self.cap_trme_leg[key] *= scale

            if 'cap_trmi_leg' in comp_slct:
                for key in self.cap_trmi_leg.sparse_keys():
                    if nd_id in key[1:3]:
                        self.cap_trmi_leg[key] *= scale








