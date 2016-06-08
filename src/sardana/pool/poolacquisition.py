#!/usr/bin/env python

##############################################################################
##
## This file is part of Sardana
##
## http://www.sardana-controls.org/
##
## Copyright 2011 CELLS / ALBA Synchrotron, Bellaterra, Spain
##
## Sardana is free software: you can redistribute it and/or modify
## it under the terms of the GNU Lesser General Public License as published by
## the Free Software Foundation, either version 3 of the License, or
## (at your option) any later version.
##
## Sardana is distributed in the hope that it will be useful,
## but WITHOUT ANY WARRANTY; without even the implied warranty of
## MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
## GNU Lesser General Public License for more details.
##
## You should have received a copy of the GNU Lesser General Public License
## along with Sardana.  If not, see <http://www.gnu.org/licenses/>.
##
##############################################################################

"""This module is part of the Python Pool libray. It defines the class for an
acquisition"""

__all__ = ["AcquisitionState", "AcquisitionMap", "PoolCTAcquisition",
           "Pool0DAcquisition", "Channel", "PoolIORAcquisition"]

__docformat__ = 'restructuredtext'

import time
import datetime
import threading

from taurus.core.util.log import DebugIt
from taurus.core.util.enumeration import Enumeration

from sardana import State, ElementType, TYPE_TIMERABLE_ELEMENTS
from sardana.sardanathreadpool import get_thread_pool
from sardana.pool import SynchParam, SynchDomain, AcqSynch
from sardana.pool.poolutil import is_software_tg
from sardana.pool.poolaction import ActionContext, PoolActionItem, PoolAction
from sardana.pool.pooltriggergate import TGEventType
from sardana.pool.pooltggeneration import PoolTGGeneration

#: enumeration representing possible motion states
AcquisitionState = Enumeration("AcquisitionState", (\
    "Stopped",
#    "StoppedOnError",
#    "StoppedOnAbort",
    "Acquiring",
    "Invalid"))

AS = AcquisitionState
AcquiringStates = AS.Acquiring,
StoppedStates = AS.Stopped,  #MS.StoppedOnError, MS.StoppedOnAbort

AcquisitionMap = {
    #AS.Stopped           : State.On,
    AS.Acquiring         : State.Moving,
    AS.Invalid           : State.Invalid,
}

def split_MGConfigurations(mg_cfg_in):
    """Split MeasurementGroup configuration with channels
    triggered by SW Trigger and channels triggered by HW trigger"""

    ctrls_in = mg_cfg_in['controllers']
    mg_sw_cfg_out = {}
    mg_0d_cfg_out = {}
    mg_hw_cfg_out = {}
    mg_sw_cfg_out['controllers'] = ctrls_sw_out = {}
    mg_0d_cfg_out['controllers'] = ctrls_0d_out = {}
    mg_hw_cfg_out['controllers'] = ctrls_hw_out = {}
    for ctrl, ctrl_info in ctrls_in.items():
        # splitting ZeroD based on the type
        if ctrl.get_ctrl_types()[0] == ElementType.ZeroDExpChannel:
            ctrls_0d_out[ctrl] = ctrl_info
        # splitting rest of the channels based on the assigned trigger
        else:
            tg_element = ctrl_info.get('trigger_element')
            if tg_element is None or is_software_tg(tg_element):
                ctrls_sw_out[ctrl] = ctrl_info
            else:
                ctrls_hw_out[ctrl] = ctrl_info
    # TODO: timer and monitor are just random elements!!!
    if len(ctrls_sw_out):
        mg_sw_cfg_out['timer'] = ctrls_sw_out.values()[0]['timer']
        mg_sw_cfg_out['monitor'] = ctrls_sw_out.values()[0]['monitor']
    if len(ctrls_hw_out):
        mg_hw_cfg_out['timer'] = ctrls_hw_out.values()[0]['timer']
        mg_hw_cfg_out['monitor'] = ctrls_hw_out.values()[0]['monitor']
    return (mg_hw_cfg_out, mg_sw_cfg_out, mg_0d_cfg_out)

def getTGConfiguration(MGcfg):
    '''Build TG configuration from complete MG configuration.

    :param MGcfg: configuration dictionary of the whole Measurement Group.
    :type MGcfg: dict<>
    :return: a configuration dictionary of TG elements organized by controller
    :rtype: dict<>
    '''

    # Create list with not repeated elements
    _tg_element_list = []

    for ctrl in MGcfg["controllers"]:
        tg_element = MGcfg["controllers"][ctrl].get('trigger_element', None)
        if (tg_element != None and tg_element not in _tg_element_list):
            _tg_element_list.append(tg_element)

    # Intermediate dictionary to organize each ctrl with its elements.
    ctrl_tgelem_dict = {}
    for tgelem in _tg_element_list:
        tg_ctrl = tgelem.get_controller()
        if tg_ctrl not in ctrl_tgelem_dict.keys():
            ctrl_tgelem_dict[tg_ctrl] = [tgelem]
        else:
            ctrl_tgelem_dict[tg_ctrl].append(tgelem)

    # Build TG configuration dictionary.
    TGcfg = {}
    TGcfg['controllers'] = {}

    for ctrl in ctrl_tgelem_dict:
        TGcfg['controllers'][ctrl] = ctrls = {}
        ctrls['channels'] = {}
        for tg_elem in ctrl_tgelem_dict[ctrl]:
            ch = ctrls['channels'][tg_elem] = {}
            ch['full_name']= tg_elem.full_name
    #TODO: temporary returning tg_elements
    return TGcfg, _tg_element_list

def extract_integ_time(synchronization):
    """Extract integration time(s) from synchronization dict. If there is onl 
    one group in the synchronization than returns float with the integration
    time. Otherwise a list of floats with different integration times.

    :param synchronization: group(s) where each group is described by
        SynchParam(s)
    :type synchronization: list(dict)
    :return list(float) or float
    """
    if len(synchronization) == 1:
        integ_time = synchronization[0][SynchParam.Active][SynchDomain.Time]
    else:
        for group in synchronization:
            active_time = group[SynchParam.Active][SynchDomain.Time]
            repeats = group[SynchParam.Repeats]
            integ_time += [active_time] * repeats
    return integ_time


class PoolAcquisition(PoolAction):

    def __init__(self, main_element, name="Acquisition"):
        PoolAction.__init__(self, main_element, name)
        zerodname = name + ".0DAcquisition"
        hwname = name + ".HardwareAcquisition"
        swname = name + ".SoftwareAcquisition"
        tgname = name + ".TGGeneration"

        self._sw_acq_config = None
        self._0d_config = None
        self._sw_acq_busy = threading.Event()
        self._0d_acq_busy = threading.Event()
        self._sw_acq = PoolAcquisitionSoftware(main_element, name=swname)
        self._hw_acq = PoolAcquisitionHardware(main_element, name=hwname)
        self._0d_acq = Pool0DAcquisition(main_element, name=zerodname)
        self._tg_gen = PoolTGGeneration(main_element, name=tgname)


    def set_sw_config(self, config):
        self._sw_acq_config = config

    def set_0d_config(self, config):
        self._0d_config = config

    #TODO: use is running flag instead
    def set_sw_acq_busy(self, busy):
        '''Callback to reset busy event about the continuous count acquisition.
        It is triggered by the WorkerThread when the acquisition has finished.
        '''
        if busy is True:
            self._sw_acq_busy.set()
        else:
            self._sw_acq_busy.clear()

    def set_0d_acq_busy(self, busy):
        '''Callback to reset busy event about the continuous count acquisition.
        It is triggered by the WorkerThread when the acquisition has finished.
        '''
        if busy is True:
            self._0d_acq_busy.set()
        else:
            self._0d_acq_busy.clear()

    def is_cont_ct_acq_busy(self):
        '''Verify if the continuous count acquisition is busy
        '''
        return self._sw_acq_busy.is_set()

    def is_zerod_acq_busy(self):
        '''Verify if the zerod acquisition is busy
        '''
        return self._0d_acq_busy.is_set()

    def event_received(self, *args, **kwargs):
        timestamp = time.time()
        _, event_type, event_id = args
        t_fmt = '%Y-%m-%d %H:%M:%S.%f'
        t_str = datetime.datetime.fromtimestamp(timestamp).strftime(t_fmt)
        self.debug('%s event with id: %d received at: %s' %\
                             (TGEventType.whatis(event_type), event_id, t_str))
        if event_type == TGEventType.Active:
            # this code is not thread safe, but for the moment we assume that
            # only one EventGenerator will work at the same time
            if self._sw_acq_config:
                if self.is_cont_ct_acq_busy():
                    msg = ('Skipping trigger: software acquisition is still'
                           ' in progress.')
                    self.debug(msg)
                    return
                else:
                    self.set_sw_acq_busy(True)
                    self.debug('Executing software acquisition.')
                    args = ()
                    kwargs = self._sw_acq_config
                    kwargs['synch'] = True
                    kwargs['idx'] = event_id
                    get_thread_pool().add(self._run_ct_continuous,
                                          callback=self.set_sw_acq_busy,
                                          *args,
                                          **kwargs)
            if self._0d_config:
                if self.is_zerod_acq_busy():
                    msg = ('Skipping trigger: ZeroD acquisition is still in'
                           ' progress.')
                    self.debug(msg)
                    return
                else:
                    self.set_0d_acq_busy(True)
                    self.debug('Executing ZeroD acquisition.')
                    args = ()
                    kwargs = self._0d_config
                    kwargs['synch'] = True
                    kwargs['idx'] = event_id
                    get_thread_pool().add(self._run_zerod_acquisition,
                                          callback=self.set_0d_acq_busy,
                                          *args,
                                          **kwargs)

        elif event_type == TGEventType.Passive:
            if self._0d_config and self.is_zerod_acq_busy():
                self.debug('Stopping ZeroD acquisition.')
                self._0d_acq.stop_action()

    def is_running(self):
        return self._0d_acq.is_running() or\
               self._sw_acq.is_running() or\
               self._hw_acq.is_running() or\
               self._tg_gen.is_running()

    def run(self, *args, **kwargs):
        config = kwargs['config']
        synchronization = kwargs["synchronization"]
        integ_time = extract_integ_time(synchronization)
        # TODO: this code splits the global mg configuration into 
        # experimental channels triggered by hw and experimental channels
        # triggered by sw. Refactor it!!!!
        (hw_acq_cfg, sw_acq_cfg, zerod_acq_cfg) = split_MGConfigurations(config)
        tg_cfg, _ = getTGConfiguration(config)
        # starting continuous acquisition only if there are any controllers
        if len(hw_acq_cfg['controllers']):
            cont_acq_kwargs = dict(kwargs)
            cont_acq_kwargs['config'] = hw_acq_cfg
            cont_acq_kwargs['integ_time'] = integ_time
            self._hw_acq.run(*args, **cont_acq_kwargs)
        if len(sw_acq_cfg['controllers']) or len(zerod_acq_cfg['controllers']):
            self._tg_gen.add_listener(self)
            if len(sw_acq_cfg['controllers']):
                sw_acq_kwargs = dict(kwargs)
                sw_acq_kwargs['config'] = sw_acq_cfg
                sw_acq_kwargs['integ_time'] = integ_time
                self.set_sw_config(sw_acq_kwargs)
            if len(zerod_acq_cfg['controllers']):
                zerod_acq_kwargs = dict(kwargs)
                zerod_acq_kwargs['config'] = zerod_acq_cfg
                self.set_0d_config(zerod_acq_kwargs)
        tg_kwargs = dict(kwargs)
        tg_kwargs['config'] = tg_cfg
        self._tg_gen.run(*args, **tg_kwargs)

    def _run_ct_continuous(self, *args, **kwargs):
        """Run a single acquisition with the software triggered elements
        during the continuous acquisition
        """
        try:
            self._sw_acq.run(*args, **kwargs)
        except:
            self.error('Continuous Count Acquisition has failed')
            self.debug('Details:', exc_info=True)
        finally:
            # return False indicating that the acquisition is not busy 
            return False

    def _run_zerod_acquisition(self, *args, **kwargs):
        """Run a single acquisition with the software triggered elements
        during the continuous acquisition
        """
        try:
            self._0d_acq.run(*args, **kwargs)
        except:
            self.error('ZeroD Acquisition has failed')
            self.error('Details:', exc_info=True)
        finally:
            # return False indicating that the acquisition is not busy 
            return False

    def _get_action_for_element(self, element):
        elem_type = element.get_type()
        if elem_type in TYPE_TIMERABLE_ELEMENTS:
            main_element = self.main_element
            channel_to_acq_synch = main_element._channel_to_acq_synch
            acq_synch = channel_to_acq_synch.get(element)
            if acq_synch in (AcqSynch.SoftwareTrigger,
                             AcqSynch.SoftwareGate):
                return self._sw_acq
            elif acq_synch in (AcqSynch.HardwareTrigger,
                               AcqSynch.HardwareGate):
                return self._hw_acq
            else:
                # by default software synchronization is in use
                return self._sw_acq
        elif elem_type == ElementType.ZeroDExpChannel:
            return self._0d_acq
        elif elem_type == ElementType.TriggerGate:
            return self._tg_gen
        else:
            raise RuntimeError("Could not determine action for element %s" %
                               element)

    def clear_elements(self):
        """Clears all elements from this action"""

    def add_element(self, element):
        """Adds a new element to this action.

        :param element: the new element to be added
        :type element: sardana.pool.poolelement.PoolElement"""
        action = self._get_action_for_element(element)
        action.add_element(element)

    def remove_element(self, element):
        """Removes an element from this action. If the element is not part of
        this action, a ValueError is raised.

        :param element: the new element to be removed
        :type element: sardana.pool.poolelement.PoolElement

        :raises: ValueError"""
        for action in self._get_acq_for_element(element):
            action.remove_element(element)        

    def get_elements(self, copy_of=False):
        """Returns a sequence of all elements involved in this action.

        :param copy_of: If False (default) the internal container of elements is
                        returned. If True, a copy of the internal container is
                        returned instead
        :type copy_of: bool
        :return: a sequence of all elements involved in this action.
        :rtype: seq<sardana.pool.poolelement.PoolElement>"""
        #TODO: this is broken now - fix it
        return self._ct_acq.get_elements() + self._0d_acq.get_elements()

    def get_pool_controller_list(self):
        """Returns a list of all controller elements involved in this action.

        :return: a list of all controller elements involved in this action.
        :rtype: list<sardana.pool.poolelement.PoolController>"""
        return self._pool_ctrl_list

    def get_pool_controllers(self):
        """Returns a dict of all controller elements involved in this action.

        :return: a dict of all controller elements involved in this action.
        :rtype: dict<sardana.pool.poolelement.PoolController, seq<sardana.pool.poolelement.PoolElement>>"""
        ret = {}
        ret.update(self._hw_acq.get_pool_controllers())
        ret.update(self._sw_acq.get_pool_controllers())
        ret.update(self._0d_acq.get_pool_controllers())
        return ret

    def read_value(self, ret=None, serial=False):
        """Reads value information of all elements involved in this action

        :param ret: output map parameter that should be filled with value
                    information. If None is given (default), a new map is
                    created an returned
        :type ret: dict
        :param serial: If False (default) perform controller HW value requests
                       in parallel. If True, access is serialized.
        :type serial: bool
        :return: a map containing value information per element
        :rtype: dict<:class:~`sardana.pool.poolelement.PoolElement`,
                     :class:~`sardana.sardanavalue.SardanaValue`>"""
        #TODO: this is broken now - fix it
        ret = self._ct_acq.read_value(ret=ret, serial=serial)
        ret.update(self._0d_acq.read_value(ret=ret, serial=serial))
        return ret


class Channel(PoolActionItem):

    def __init__(self, acquirable, info=None):
        PoolActionItem.__init__(self, acquirable)
        if info:
            self.__dict__.update(info)

    def __getattr__(self, name):
        return getattr(self.element, name)


class PoolAcquisitionBase(PoolAction):

    def __init__(self, main_element, name):
        PoolAction.__init__(self, main_element, name)
        self._channels = None

    def in_acquisition(self, states):
        """Determines if we are in acquisition or if the acquisition has ended
        based on the current unit trigger modes and states returned by the
        controller(s)

        :param states: a map containing state information as returned by
                       read_state_info
        :type states: dict<PoolElement, State>
        :return: returns True if in acquisition or False otherwise
        :rtype: bool"""
        for elem in states:
            s = states[elem][0][0]
            if self._is_in_action(s):
                return True

    @DebugIt()
    def start_action(self, *args, **kwargs):
        """Prepares everything for acquisition and starts it.
        :param acq_sleep_time: sleep time between state queries
        :param nb_states_per_value: how many state queries between readouts
        :param integ_time: integration time(s)
        :type integ_time: float or seq<float>
        :param repetitions: repetitions
        :type repetitions: int
        :param config: configuration dictionary (with information about
            involved controllers and channels)
        """
        pool = self.pool

        self._aborted = False
        self._stopped = False

        self._acq_sleep_time = kwargs.pop("acq_sleep_time",
                                          pool.acq_loop_sleep_time)
        self._nb_states_per_value = kwargs.pop("nb_states_per_value",
                                               pool.acq_loop_states_per_value)

        self._integ_time = integ_time = kwargs.get("integ_time")
        self._mon_count = mon_count = kwargs.get("monitor_count")
        self._repetitions = repetitions = kwargs.get("repetitions")
        if integ_time is None and mon_count is None:
            raise Exception("must give integration time or monitor counts")
        if integ_time is not None and mon_count is not None:
            msg = ("must give either integration time or monitor counts "
                   "(not both)")
            raise Exception(msg)

        _ = kwargs.get("items", self.get_elements())
        cfg = kwargs['config']
        # determine which is the controller which holds the master channel

        if integ_time is not None:
            master_key = 'timer'
            master_value = integ_time
        if mon_count is not None:
            master_key = 'monitor'
            master_value = -mon_count

        master = cfg[master_key]
        master_ctrl = master.controller

        pool_ctrls_dict = dict(cfg['controllers'])
        pool_ctrls_dict.pop('__tango__', None)
        pool_ctrls = []
        self._pool_ctrl_dict_loop = _pool_ctrl_dict_loop = {}
        for ctrl, v in pool_ctrls_dict.items():
            if ctrl.is_timerable():
                pool_ctrls.append(ctrl)
            if ElementType.CTExpChannel in ctrl.get_ctrl_types():
                _pool_ctrl_dict_loop[ctrl] = v

        # make sure the controller which has the master channel is the last to
        # be called
        pool_ctrls.remove(master_ctrl)
        pool_ctrls.append(master_ctrl)

        # Determine which channels are active
        self._channels = channels = {}
        for pool_ctrl in pool_ctrls:
            ctrl = pool_ctrl.ctrl
            pool_ctrl_data = pool_ctrls_dict[pool_ctrl]
            elements = pool_ctrl_data['channels']

            for element, element_info in elements.items():
                axis = element.axis
                channel = Channel(element, info=element_info)
                channels[element] = channel

        with ActionContext(self):

            # PreLoadAll, PreLoadOne, LoadOne and LoadAll
            for pool_ctrl in pool_ctrls:
                ctrl = pool_ctrl.ctrl
                pool_ctrl_data = pool_ctrls_dict[pool_ctrl]
                ctrl.PreLoadAll()
                master = pool_ctrl_data[master_key]
                axis = master.axis
                try:
                    res = ctrl.PreLoadOne(axis, master_value, repetitions)
                except TypeError:
                    #TODO: raise correctly deprecation warning
                    self.warning("PreLoadOne API has changed")
                    res = ctrl.PreLoadOne(axis, master_value)
                if not res:
                    msg = ("%s.PreLoadOne(%d) returned False" %
                           (pool_ctrl.name, axis))
                    raise Exception(msg)
                try:
                    ctrl.LoadOne(axis, master_value, repetitions)
                except TypeError:
                    #TODO: raise correctly deprecation warning
                    self.warning("LoadOne API has changed")
                    ctrl.LoadOne(axis, master_value)
                ctrl.LoadAll()

            # PreStartAll on all controllers
            for pool_ctrl in pool_ctrls:
                pool_ctrl.ctrl.PreStartAll()

            # PreStartOne & StartOne on all elements
            for pool_ctrl in pool_ctrls:
                ctrl = pool_ctrl.ctrl
                pool_ctrl_data = pool_ctrls_dict[pool_ctrl]
                elements = pool_ctrl_data['channels'].keys()
                timer_monitor = pool_ctrl_data[master_key]
                # make sure that the timer/monitor is started as the last one
                elements.remove(timer_monitor)
                elements.append(timer_monitor)
                for element in elements:
                    axis = element.axis
                    channel = channels[element]
                    if channel.enabled:
                        ret = ctrl.PreStartOne(axis, master_value)
                        if not ret:
                            msg = ("%s.PreStartOne(%d) returns False" %
                                   (pool_ctrl.name, axis))
                            raise Exception(msg)
                        ctrl.StartOne(axis, master_value)

            # set the state of all elements to  and inform their listeners
            for channel in channels:
                channel.set_state(State.Moving, propagate=2)

            # StartAll on all controllers
            for pool_ctrl in pool_ctrls:
                pool_ctrl.ctrl.StartAll()


class PoolAcquisitionHardware(PoolAcquisitionBase):

    def __init__(self, main_element, name="AcquisitionHardware"):
        PoolAcquisitionBase.__init__(self, main_element, name)

    @DebugIt()
    def action_loop(self):
        i = 0

        states, values = {}, {}
        for element in self._channels:
            states[element] = None
            values[element] = None

        nap = self._acq_sleep_time
        nb_states_per_value = self._nb_states_per_value

        # read values to send a first event when starting to acquire
        with ActionContext(self):
            self.raw_read_value_loop(ret=values)
            for acquirable, value in values.items():
                if len(value.value) > 0:
                    acquirable.put_value(value, propagate=2)

        while True:
            self.read_state_info(ret=states)
            if not self.in_acquisition(states):
                break

            # read value every n times
            if not i % nb_states_per_value:
                self.read_value_loop(ret=values)
                for acquirable, value in values.items():
                    if len(value.value) > 0:
                        acquirable.put_value(value)

            time.sleep(nap)
            i += 1

        with ActionContext(self):
            self.raw_read_state_info(ret=states)
            self.raw_read_value_loop(ret=values)

        for acquirable, state_info in states.items():
            # first update the element state so that value calculation
            # that is done after takes the updated state into account
            acquirable.set_state_info(state_info, propagate=0)
            if acquirable in values:
                value = values[acquirable]
                if len(value.value) > 0:
                    acquirable.put_value(value, propagate=2)
            with acquirable:
                acquirable.clear_operation()
                state_info = acquirable._from_ctrl_state_info(state_info)
                acquirable.set_state_info(state_info, propagate=2)


class PoolAcquisitionSoftware(PoolAcquisitionBase):

    def __init__(self, main_element, name="AcquisitionSoftware"):
        PoolAcquisitionBase.__init__(self, main_element, name)

    @DebugIt()
    def start_action(self, *args, **kwargs):
        """Prepares everything for acquisition and starts it.
        :param acq_sleep_time: sleep time between state queries
        :param nb_states_per_value: how many state queries between readouts
        :param integ_time: integration time(s)
        :type integ_time: float or seq<float>
        :param repetitions: repetitions
        :type repetitions: int
        :param config: configuration dictionary (with information about
            involved controllers and channels)
        :param index: trigger index that will be assigned to the acquired value
        :type index: int
        """
        PoolAcquisitionBase.start_action(self, *args, **kwargs)
        self.index = kwargs.get("idx")

    @DebugIt()
    def action_loop(self):
        states, values = {}, {}
        for element in self._channels:
            states[element] = None
            values[element] = None

        nap = self._acq_sleep_time

        while True:
            self.read_state_info(ret=states)
            if not self.in_acquisition(states):
                break

            time.sleep(nap)

        with ActionContext(self):
            self.raw_read_state_info(ret=states)
            self.raw_read_value_loop(ret=values)

        for acquirable, state_info in states.items():
            # first update the element state so that value calculation
            # that is done after takes the updated state into account
            acquirable.set_state_info(state_info, propagate=0)
            if acquirable in values:
                value = values[acquirable]
                # TODO: workaround in order to pass the value via Tango Data attribute
                # At this moment experimental channel values are passed via two
                # different Tango attributes (Value and Data).
                # The discrimination is  based on type of the value: if the type
                # is scalar it is passed via Value, and if type is spectrum it 
                # is passed via Data. 
                # We want to pass it via Data so we encapsulate value and index in lists.
                value.value = [value.value]
                value.idx = [self.index]
                acquirable.put_value(value, propagate=2)
            with acquirable:
                acquirable.clear_operation()
                state_info = acquirable._from_ctrl_state_info(state_info)
                acquirable.set_state_info(state_info, propagate=2)


class PoolCTAcquisition(PoolAction):

    def __init__(self, main_element, name="CTAcquisition", slaves=None):
        self._channels = None

        if slaves is None:
            slaves = ()
        self._slaves = slaves

        PoolAction.__init__(self, main_element, name)

    def get_read_value_loop_ctrls(self):
        return self._pool_ctrl_dict_loop

    @DebugIt()
    def start_action(self, *args, **kwargs):
        """Prepares everything for acquisition and starts it.

           :param: config"""        
        pool = self.pool

        # prepare data structures
        self._aborted = False
        self._stopped = False

        self.conf = kwargs

        self._acq_sleep_time = kwargs.pop("acq_sleep_time",
                                             pool.acq_loop_sleep_time)
        self._nb_states_per_value = \
            kwargs.pop("nb_states_per_value",
                       pool.acq_loop_states_per_value)

        self._integ_time = integ_time = kwargs.get("integ_time")
        self._mon_count = mon_count = kwargs.get("monitor_count")
        if integ_time is None and mon_count is None:
            raise Exception("must give integration time or monitor counts")
        if integ_time is not None and mon_count is not None:
            raise Exception("must give either integration time or monitor counts (not both)")

        _ = kwargs.get("items", self.get_elements())
        cfg = kwargs['config']
        # determine which is the controller which holds the master channel

        if integ_time is not None:
            master_key = 'timer'
            master_value = integ_time
        if mon_count is not None:
            master_key = 'monitor'
            master_value = -mon_count

        master = cfg[master_key]
        master_ctrl = master.controller

        pool_ctrls_dict = dict(cfg['controllers'])
        pool_ctrls_dict.pop('__tango__', None)
        pool_ctrls = []
        self._pool_ctrl_dict_loop = _pool_ctrl_dict_loop = {}
        for ctrl, v in pool_ctrls_dict.items():
            if ctrl.is_timerable():
                pool_ctrls.append(ctrl)
            if ElementType.CTExpChannel in ctrl.get_ctrl_types():
                _pool_ctrl_dict_loop[ctrl] = v

        # make sure the controller which has the master channel is the last to
        # be called
        pool_ctrls.remove(master_ctrl)
        pool_ctrls.append(master_ctrl)

        # Determine which channels are active
        self._channels = channels = {}
        for pool_ctrl in pool_ctrls:
            ctrl = pool_ctrl.ctrl
            pool_ctrl_data = pool_ctrls_dict[pool_ctrl]
            elements = pool_ctrl_data['channels']

            for element, element_info in elements.items():
                axis = element.axis
                channel = Channel(element, info=element_info)
                channels[element] = channel

        #for channel in channels:
        #    channel.prepare_to_acquire(self)

        with ActionContext(self):

            # PreLoadAll, PreLoadOne, LoadOne and LoadAll
            for pool_ctrl in pool_ctrls:
                ctrl = pool_ctrl.ctrl
                pool_ctrl_data = pool_ctrls_dict[pool_ctrl]
                ctrl.PreLoadAll()
                master = pool_ctrl_data[master_key]
                axis = master.axis
                res = ctrl.PreLoadOne(axis, master_value)
                if not res:
                    raise Exception("%s.PreLoadOne(%d) returns False" % (pool_ctrl.name, axis,))
                ctrl.LoadOne(axis, master_value)
                ctrl.LoadAll()

            # PreStartAll on all controllers
            for pool_ctrl in pool_ctrls:
                pool_ctrl.ctrl.PreStartAll()

            # PreStartOne & StartOne on all elements
            for pool_ctrl in pool_ctrls:
                ctrl = pool_ctrl.ctrl
                pool_ctrl_data = pool_ctrls_dict[pool_ctrl]
                elements = pool_ctrl_data['channels'].keys()
                timer_monitor = pool_ctrl_data[master_key]
                # make sure that the timer/monitor is started as the last one
                elements.remove(timer_monitor)
                elements.append(timer_monitor)
                for element in elements:
                    axis = element.axis
                    channel = channels[element]
                    if channel.enabled:
                        ret = ctrl.PreStartOne(axis, master_value)
                        if not ret:
                            raise Exception("%s.PreStartOne(%d) returns False" \
                                            % (pool_ctrl.name, axis))
                        ctrl.StartOne(axis, master_value)

            # set the state of all elements to  and inform their listeners
            for channel in channels:
                channel.set_state(State.Moving, propagate=2)

            # StartAll on all controllers
            for pool_ctrl in pool_ctrls:
                pool_ctrl.ctrl.StartAll()

    def in_acquisition(self, states):
        """Determines if we are in acquisition or if the acquisition has ended
        based on the current unit trigger modes and states returned by the
        controller(s)

        :param states: a map containing state information as returned by
                       read_state_info
        :type states: dict<PoolElement, State>
        :return: returns True if in acquisition or False otherwise
        :rtype: bool"""
        for elem in states:
            s = states[elem][0][0]
            if self._is_in_action(s):
                return True

    @DebugIt()
    def action_loop(self):
        i = 0

        states, values = {}, {}
        for element in self._channels:
            states[element] = None
            #values[element] = None

        nap = self._acq_sleep_time
        nb_states_per_value = self._nb_states_per_value

        # read values to send a first event when starting to acquire
        with ActionContext(self):
            self.raw_read_value_loop(ret=values)
            for acquirable, value in values.items():                
                acquirable.put_value(value, propagate=2)

        while True:
            self.read_state_info(ret=states)
            if not self.in_acquisition(states):
                break

            # read value every n times
            if not i % nb_states_per_value:
                self.read_value_loop(ret=values)
                for acquirable, value in values.items():                    
                    acquirable.put_value(value)

            time.sleep(nap)
            i += 1

        for slave in self._slaves:
            try:
                slave.stop_action()
            except:
                self.warning("Unable to stop slave acquisition %s",
                             slave.getLogName())
                self.debug("Details", exc_info=1)

        with ActionContext(self):
            self.raw_read_state_info(ret=states)
            self.raw_read_value_loop(ret=values)

        for acquirable, state_info in states.items():
            # first update the element state so that value calculation
            # that is done after takes the updated state into account
            acquirable.set_state_info(state_info, propagate=0)
            if acquirable in values:
                value = values[acquirable]
                acquirable.put_value(value, propagate=2)
            with acquirable:
                acquirable.clear_operation()
                state_info = acquirable._from_ctrl_state_info(state_info)
                acquirable.set_state_info(state_info, propagate=2)

class PoolContHWAcquisition(PoolCTAcquisition):

    def __init__(self, main_element, name="ContHWAcquisition", slaves=None):
        PoolCTAcquisition.__init__(self, main_element, name, slaves=slaves)

    @DebugIt()
    def start_action(self, *args, **kwargs):
        """Prepares everything for acquisition and starts it.

           :param: config"""        
        pool = self.pool

        # prepare data structures
        self._aborted = False
        self._stopped = False

        self.conf = kwargs

        self._acq_sleep_time = kwargs.pop("acq_sleep_time",
                                             pool.acq_loop_sleep_time)
        self._nb_states_per_value = \
            kwargs.pop("nb_states_per_value",
                       pool.acq_loop_states_per_value)

        self._integ_time = integ_time = kwargs.get("integ_time")
        self._mon_count = mon_count = kwargs.get("monitor_count")
        if integ_time is None and mon_count is None:
            raise Exception("must give integration time or monitor counts")
        if integ_time is not None and mon_count is not None:
            raise Exception("must give either integration time or monitor counts (not both)")

        _ = kwargs.get("items", self.get_elements())
        cfg = kwargs['config']
        # determine which is the controller which holds the master channel

        if integ_time is not None:
            master_key = 'timer'
            master_value = integ_time
        if mon_count is not None:
            master_key = 'monitor'
            master_value = -mon_count

        master = cfg[master_key]
        master_ctrl = master.controller

        pool_ctrls_dict = dict(cfg['controllers'])
        pool_ctrls_dict.pop('__tango__', None)
        pool_ctrls = []
        self._pool_ctrl_dict_loop = _pool_ctrl_dict_loop = {}
        for ctrl, v in pool_ctrls_dict.items():
            if ctrl.is_timerable():
                pool_ctrls.append(ctrl)
            if ElementType.CTExpChannel in ctrl.get_ctrl_types():
                _pool_ctrl_dict_loop[ctrl] = v

        # make sure the controller which has the master channel is the last to
        # be called
        pool_ctrls.remove(master_ctrl)
        pool_ctrls.append(master_ctrl)

        # Determine which channels are active
        self._channels = channels = {}
        for pool_ctrl in pool_ctrls:
            ctrl = pool_ctrl.ctrl
            pool_ctrl_data = pool_ctrls_dict[pool_ctrl]
            elements = pool_ctrl_data['channels']

            for element, element_info in elements.items():
                axis = element.axis
                channel = Channel(element, info=element_info)
                channels[element] = channel

        #for channel in channels:
        #    channel.prepare_to_acquire(self)

        with ActionContext(self):

            # repetitions
            repetitions = self.conf['repetitions']
            for pool_ctrl in pool_ctrls:
                ctrl = pool_ctrl.ctrl
                ctrl.SetCtrlPar('repetitions', repetitions)

            # PreLoadAll, PreLoadOne, LoadOne and LoadAll
            for pool_ctrl in pool_ctrls:
                ctrl = pool_ctrl.ctrl
                pool_ctrl_data = pool_ctrls_dict[pool_ctrl]
                ctrl.PreLoadAll()
                master = pool_ctrl_data[master_key]
                axis = master.axis
                res = ctrl.PreLoadOne(axis, master_value)
                if not res:
                    raise Exception("%s.PreLoadOne(%d) returns False" % (pool_ctrl.name, axis,))
                ctrl.LoadOne(axis, master_value)
                ctrl.LoadAll()

            # PreStartAll on all controllers
            for pool_ctrl in pool_ctrls:
                pool_ctrl.ctrl.PreStartAll()

            # PreStartOne & StartOne on all elements
            for pool_ctrl in pool_ctrls:
                ctrl = pool_ctrl.ctrl
                pool_ctrl_data = pool_ctrls_dict[pool_ctrl]
                elements = pool_ctrl_data['channels']
                for element in elements:
                    axis = element.axis
                    channel = channels[element]
                    if channel.enabled:
                        ret = ctrl.PreStartOne(axis, master_value)
                        if not ret:
                            raise Exception("%s.PreStartOne(%d) returns False" \
                                            % (pool_ctrl.name, axis))
                        ctrl.StartOne(axis, master_value)

            # set the state of all elements to  and inform their listeners
            for channel in channels:
                channel.set_state(State.Moving, propagate=2)

            # StartAll on all controllers
            for pool_ctrl in pool_ctrls:
                pool_ctrl.ctrl.StartAll()
        
    @DebugIt()
    def action_loop(self):
        i = 0

        states, values = {}, {}
        for element in self._channels:
            states[element] = None
            #values[element] = None

        nap = self._acq_sleep_time
        nb_states_per_value = self._nb_states_per_value

        # read values to send a first event when starting to acquire
        with ActionContext(self):
            self.raw_read_value_loop(ret=values)
            for acquirable, value in values.items():
                #TODO: This is a protection to avoid invalid types.
                # Uncomment these lines
                #if not isinstance(value.value, list):
                #     continue
                if len(value.value) > 0:
                    acquirable.put_value(value, propagate=2)

        while True:
            self.read_state_info(ret=states)
            if not self.in_acquisition(states):
                break

            # read value every n times
            if not i % nb_states_per_value:
                self.read_value_loop(ret=values)
                for acquirable, value in values.items():
                    #TODO: This is a protection to avoid invalid types.
                    # Uncomment these lines
                    #if not isinstance(value.value, list):
                    #    continue
                    if len(value.value) > 0:
                        acquirable.put_value(value)

            time.sleep(nap)
            i += 1

        for slave in self._slaves:
            try:
                slave.stop_action()
            except:
                self.warning("Unable to stop slave acquisition %s",
                             slave.getLogName())
                self.debug("Details", exc_info=1)

        with ActionContext(self):
            self.raw_read_state_info(ret=states)
            self.raw_read_value_loop(ret=values)

        for acquirable, state_info in states.items():
            # first update the element state so that value calculation
            # that is done after takes the updated state into account
            acquirable.set_state_info(state_info, propagate=0)
            if acquirable in values:
                value = values[acquirable]
                #TODO: This is a protection to avoid invalid types.
                # Uncomment these lines
                #if not isinstance(value.value, list):
                #    continue
                if len(value.value) > 0:
                    acquirable.put_value(value, propagate=2)
            with acquirable:
                acquirable.clear_operation()
                state_info = acquirable._from_ctrl_state_info(state_info)
                acquirable.set_state_info(state_info, propagate=2)


class PoolContSWCTAcquisition(PoolCTAcquisition):

    def __init__(self, main_element, name="CTAcquisition", slaves=None):
        PoolCTAcquisition.__init__(self, main_element, name="CTAcquisition", slaves=None)

    @DebugIt()
    def action_loop(self):
        i = 0

        states, values = {}, {}
        for element in self._channels:
            states[element] = None
            #values[element] = None

        nap = self._acq_sleep_time
        nb_states_per_value = self._nb_states_per_value

        # read values to send a first event when starting to acquire
#         with ActionContext(self):
#             self.raw_read_value_loop(ret=values)
#             for acquirable, value in values.items():
#                 acquirable.put_value(value, propagate=2)

        while True:
            self.read_state_info(ret=states)
            if not self.in_acquisition(states):
                break

            # read value every n times
#             if not i % nb_states_per_value:
#                 self.read_value_loop(ret=values)
#                 for acquirable, value in values.items():
#                     acquirable.put_value(value)

            time.sleep(nap)
#             i += 1

        for slave in self._slaves:
            try:
                slave.stop_action()
            except:
                self.warning("Unable to stop slave acquisition %s",
                             slave.getLogName())
                self.debug("Details", exc_info=1)

        with ActionContext(self):
            self.raw_read_state_info(ret=states)
            self.raw_read_value_loop(ret=values)

        for acquirable, state_info in states.items():
            # first update the element state so that value calculation
            # that is done after takes the updated state into account
            acquirable.set_state_info(state_info, propagate=0)
            if acquirable in values:
                value = values[acquirable]
                # TODO: workaround in order to pass the value via Tango Data attribute
                # At this moment experimental channel values are passed via two
                # different Tango attributes (Value and Data).
                # The discrimination is  based on type of the value: if the type
                # is scalar it is passed via Value, and if type is spectrum it 
                # is passed via Data. 
                # We want to pass it via Data so we encapsulate value and index in lists.
                value.value = [value.value]
                value.idx = [self.conf['idx']]
                acquirable.put_value(value, propagate=2)
            with acquirable:
                acquirable.clear_operation()
                state_info = acquirable._from_ctrl_state_info(state_info)
                acquirable.set_state_info(state_info, propagate=2)


class Pool0DAcquisition(PoolAction):

    def __init__(self, main_element, name="0DAcquisition"):
        self._channels = None
        PoolAction.__init__(self, main_element, name)

    def start_action(self, *args, **kwargs):
        """Prepares everything for acquisition and starts it.

           :param: config"""

        pool = self.pool

        self.conf = kwargs

        # prepare data structures
        self._aborted = False
        self._stopped = False

        self._acq_sleep_time = kwargs.pop("acq_sleep_time",
                                          pool.acq_loop_sleep_time)
        self._nb_states_per_value = \
            kwargs.pop("nb_states_per_value",
                       pool.acq_loop_states_per_value)

        items = kwargs.get("items")
        if items is None:
            items = self.get_elements()
        cfg = kwargs['config']

        pool_ctrls_dict = dict(cfg['controllers'])
        pool_ctrls_dict.pop('__tango__', None)
        pool_ctrls = []
        for ctrl in pool_ctrls_dict:
            if ElementType.ZeroDExpChannel in ctrl.get_ctrl_types():
                pool_ctrls.append(ctrl)

        # Determine which channels are active
        self._channels = channels = {}
        for pool_ctrl in pool_ctrls:
            ctrl = pool_ctrl.ctrl
            pool_ctrl_data = pool_ctrls_dict[pool_ctrl]
            elements = pool_ctrl_data['channels']

            for element, element_info in elements.items():
                channel = Channel(element, info=element_info)
                channels[element] = channel

        with ActionContext(self):
            # set the state of all elements to  and inform their listeners
            for channel in channels:
                channel.clear_buffer()
                channel.set_state(State.Moving, propagate=2)

    def in_acquisition(self, states):
        """Determines if we are in acquisition or if the acquisition has ended
        based on the current unit trigger modes and states returned by the
        controller(s)

        :param states: a map containing state information as returned by
                       read_state_info
        :type states: dict<PoolElement, State>
        :return: returns True if in acquisition or False otherwise
        :rtype: bool"""
        for state in states:
            s = states[state][0]
            if self._is_in_action(s):
                return True

    def action_loop(self):
        states, values = {}, {}
        for element in self._channels:
            states[element] = None
            values[element] = None

        nap = self._acq_sleep_time
        while True:
            self.read_value(ret=values)
            for acquirable, value in values.items():
                acquirable.put_value(value, index=self.conf['idx'], propagate=0)
            if self._stopped or self._aborted:
                break
            time.sleep(nap)

        for element in self._channels:
            element.propagate_value(priority=1)

        with ActionContext(self):
            self.raw_read_state_info(ret=states)

        for acquirable, state_info in states.items():
            # first update the element state so that value calculation
            # that is done after takes the updated state into account
            state_info = acquirable._from_ctrl_state_info(state_info)
            acquirable.set_state_info(state_info, propagate=0)
            with acquirable:
                acquirable.clear_operation()
                acquirable.set_state_info(state_info, propagate=2)

    def stop_action(self, *args, **kwargs):
        """Stop procedure for this action."""
        self._stopped = True

    def abort_action(self, *args, **kwargs):
        """Aborts procedure for this action"""
        self._aborted = True


class PoolIORAcquisition(PoolAction):

    def __init__(self, pool, name="IORAcquisition"):
        self._channels = None
        PoolAction.__init__(self, pool, name)

    def start_action(self, *args, **kwargs):
        pass

    def in_acquisition(self, states):
        return True
        pass

    @DebugIt()
    def action_loop(self):
        i = 0

        states, values = {}, {}
        for element in self._channels:
            states[element] = None
            values[element] = None

        # read values to send a first event when starting to acquire
        self.read_value(ret=values)
        for acquirable, value in values.items():
            acquirable.put_value(value, propagate=2)

        while True:
            self.read_state_info(ret=states)

            if not self.in_acquisition(states):
                break

            # read value every n times
            if not i % 5:
                self.read_value(ret=values)
                for acquirable, value in values.items():
                    acquirable.put_value(value)

            i += 1
            time.sleep(0.01)

        self.read_state_info(ret=states)

        # first update the element state so that value calculation
        # that is done after takes the updated state into account
        for acquirable, state_info in states.items():
            acquirable.set_state_info(state_info, propagate=0)

        # Do NOT send events before we exit the OperationContext, otherwise
        # we may be asked to start another action before we leave the context
        # of the current action. Instead, send the events in the finish hook
        # which is executed outside the OperationContext

        def finish_hook(*args, **kwargs):
            # read values and propagate the change to all listeners
            self.read_value(ret=values)
            for acquirable, value in values.items():
                acquirable.put_value(value, propagate=2)

            # finally set the state and propagate to all listeners
            for acquirable, state_info in states.items():
                acquirable.set_state_info(state_info, propagate=2)

        self.set_finish_hook(finish_hook)
