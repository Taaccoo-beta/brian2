import numpy as np
import weakref

from brian2.core.base import BrianObject
from brian2.core.namespace import create_namespace
from brian2.core.preferences import brian_prefs
from brian2.core.specifiers import (ArrayVariable, Index, ReadOnlyValue, 
                                    AttributeValue, Subexpression,
                                    StochasticVariable)
from brian2.codegen.languages import PythonLanguage
from brian2.equations.equations import (Equations, DIFFERENTIAL_EQUATION,
                                        STATIC_EQUATION, PARAMETER)
from brian2.groups.group import Group, GroupCodeRunner
from brian2.memory.dynamicarray import DynamicArray1D
from brian2.stateupdaters.base import StateUpdateMethod
from brian2.units.fundamentalunits import Unit
from brian2.units.allunits import second
from brian2.utils.logger import get_logger

from .spikequeue import SpikeQueue

__all__ = ['Synapses']

logger = get_logger(__name__)

class StateUpdater(GroupCodeRunner):
    '''
    The `GroupCodeRunner` that updates the state variables of a `Synapses`
    at every timestep.
    '''
    def __init__(self, group, method):
        self.method_choice = method
        indices = {'_neuron_idx': Index('_neuron_idx', True)}
        GroupCodeRunner.__init__(self, group,
                                       group.language.template_state_update,
                                       indices=indices,
                                       when=(group.clock, 'groups'),
                                       name=group.name + '_stateupdater',
                                       check_units=False,
                                       additional_specifiers=['_num_neurons'])

        self.method = StateUpdateMethod.determine_stateupdater(self.group.equations,
                                                               self.group.namespace,
                                                               self.group.specifiers,
                                                               method)
    
    def update_abstract_code(self):        
        
        self.method = StateUpdateMethod.determine_stateupdater(self.group.equations,
                                                               self.group.namespace,
                                                               self.group.specifiers,
                                                               self.method_choice)
        
        self.abstract_code = self.method(self.group.equations,
                                         self.group.namespace,
                                         self.group.specifiers)


class TargetUpdater(GroupCodeRunner):
    '''
    The `GroupCodeRunner` that applies the pre/post statement(s) to the state
    variables of synapses where the pre-/postsynaptic group spiked in this
    time step.
    '''
    def __init__(self, synapses, prepost='pre'):
        self.prepost = prepost
        self.synapses = synapses
        indices = {'_neuron_idx': Index('_neuron_idx', False),
                   '_postsynaptic_idx': Index('_postsynaptic_idx', False),
                   '_presynaptic_idx': Index('_presynaptic_idx', False)}
        GroupCodeRunner.__init__(self, synapses,
                                 synapses.language.template_synapses,
                                 indices=indices,
                                 when=(synapses.clock, 'synapses'),
                                 name=synapses.name + '_' + prepost,
                                 additional_specifiers=['_num_neurons',
                                                        '_presynaptic',
                                                        '_postsynaptic'])        
    
    def update(self, **kwds):
        # TODO: This should really be in pre_update but then we would have
        # to propagate the information somehow to the update method -- we
        # cannot easily use the specifier mechanism as the specifiers are all
        # defined in the Synapses and we want to have the same symbol in the
        # code (_spiking_synapses) that refers to either pre- or postsynaptic
        # spikes.
        queue = self.synapses._queues[self.prepost]
        spikes = queue.peek()
        self.spikes = spikes
        queue.next()
        spikes = self.spikes
        GroupCodeRunner.update(self, _spiking_synapses=spikes,
                               _num_spiking_synapses=len(spikes))
    
    def update_abstract_code(self):
        self.abstract_code = self.synapses.code[self.prepost]


class Synapses(BrianObject, Group):

    basename = 'synapses'    
    def __init__(self, source, target=None, equations=None, pre=None, post=None,
                 namespace=None, dtype=None, language=None,
                 max_delay=0*second, clock=None, method=None, name=None):
        
        BrianObject.__init__(self, when=clock, name=name)

        if not hasattr(source, 'spikes') and hasattr(source, 'clock'):
            raise TypeError(('Source has to be a SpikeSource with spikes and'
                             ' clock attribute. Is type %r instead')
                            % type(source))

        self.source = weakref.proxy(source)
        self.target = weakref.proxy(target)
            
        ##### Prepare and validate equations
        if isinstance(equations, basestring):
            equations = Equations(equations)
        if not isinstance(equations, Equations):
            raise TypeError(('equations has to be a string or an Equations '
                             'object, is "%s" instead.') % type(equations))

        # Check flags
        equations.check_flags({DIFFERENTIAL_EQUATION: ('event-driven'),
                               PARAMETER: ('constant')})
        
        self.equations = equations

        ##### Setup the memory
        self.arrays = self._allocate_memory(dtype=dtype)

        # Setup the namespace
        self.namespace = create_namespace(1, namespace)  #FIXME

        # Code generation (TODO: this should be refactored and modularised)
        # Temporary, set default language to Python
        if language is None:
            language = PythonLanguage()
        self.language = language
        
        
        # Pre and postsynaptic synapses (i->synapse indexes)
        max_synapses=2147483647 # it could be explicitly reduced by a keyword
        

        self.pre_updater = None
        self.post_updater = None
        
        self.N = 0
        
        self._queues = {}
        self.code = {}
        self._delays = {}
        self._synapses = {}
        
        self._synapses['pre'] = [DynamicArray1D(0, dtype=smallest_inttype(max_synapses))
                                 for _ in xrange(len(self.source))]
        self._synapses['post'] = [DynamicArray1D(0, dtype=smallest_inttype(max_synapses))
                                  for _ in xrange(len(self.target))]
        
        self._indices = {}
        self._indices['pre'] = DynamicArray1D(0, dtype=smallest_inttype(max_synapses))
        self._indices['post'] = DynamicArray1D(0, dtype=smallest_inttype(max_synapses))
        
        if pre:
            self._delays['pre'] = DynamicArray1D(0, dtype=np.int16)
            self.code['pre'] = pre
            self._queues['pre'] = SpikeQueue(source, self._synapses['pre'], self._delays['pre'])
        if post:
            self._delays['post'] = DynamicArray1D(0, dtype=np.int16)            
            self.code['post'] = post
            self._queues['pre'] = SpikeQueue(source, self._synapses['post'], self._synapses['post'])            
        
        # Setup specifiers
        self.specifiers = self._create_specifiers()
        
        self.targetupdater = {}
        for prepost in self.code:
            self.targetupdater[prepost] = TargetUpdater(self, prepost)
        
        #: Performs numerical integration step
        self.state_updater = StateUpdater(self, method)
        
        self.contained_objects.append(self.state_updater)
        for updater in self.targetupdater.itervalues():
            self.contained_objects.append(updater)        
        
        # Activate name attribute access
        Group.__init__(self)

    def _create_specifiers(self):
        '''
        Create the specifiers dictionary for this `NeuronGroup`, containing
        entries for the equation variables and some standard entries.
        '''
        # Add all the pre and post specifiers with _pre and _post suffixes
        s = {}
        for name, spec in self.source.specifiers.iteritems():
            if isinstance(spec, ArrayVariable):
                new_spec = ArrayVariable(spec.name, spec.unit, spec.dtype,
                                         spec.array, '_presynaptic_idx')
                s[name + '_pre'] = new_spec
        for name, spec in self.target.specifiers.iteritems():
            if isinstance(spec, ArrayVariable):
                new_spec = ArrayVariable(spec.name, spec.unit, spec.dtype,
                             spec.array, '_postsynaptic_idx')
                s[name + '_post'] = new_spec            
                # Also add all the post specifiers without a suffix -- if this clashes
                # with the name of a state variable definined in this Synapses group,
                # the latter will overwrite the entry later and take precedence
                s[name] = new_spec
        
        # Standard specifiers always present
        s.update({'t': AttributeValue('t',  second, np.float64,
                                      self.clock, 't_'),
                  'dt': AttributeValue('dt', second, np.float64,
                                       self.clock, 'dt_', constant=True),
                  '_num_neurons': AttributeValue('_num_neurons', Unit(1), np.int,
                                                self, 'N', constant=True),
                  '_presynaptic': ArrayVariable('_presynaptic', Unit(1),
                                                np.int32, self._indices['pre'],
                                                '_presynaptic_idx'),
                  '_postsynaptic': ArrayVariable('_postsynaptic', Unit(1),
                                                np.int32, self._indices['post'],
                                                '_postsynaptic_idx')})

        for eq in self.equations.itervalues():
            if eq.type in (DIFFERENTIAL_EQUATION, PARAMETER):
                array = self.arrays[eq.varname]
                constant = ('constant' in eq.flags)
                s.update({eq.varname: ArrayVariable(eq.varname,
                                                    eq.unit,
                                                    array.dtype,
                                                    array,
                                                    '_neuron_idx',
                                                    constant)})
        
            elif eq.type == STATIC_EQUATION:
                s.update({eq.varname: Subexpression(eq.varname, eq.unit,
                                                    brian_prefs['core.default_scalar_dtype'],
                                                    str(eq.expr),
                                                    s,
                                                    self.namespace)})
            else:
                raise AssertionError('Unknown type of equation: ' + eq.eq_type)

        # Stochastic variables
        for xi in self.equations.stochastic_variables:
            s.update({xi: StochasticVariable(xi)})

        return s

    def _allocate_memory(self, dtype=None):
        # Allocate memory (TODO: this should be refactored somewhere at some point)
        arrayvarnames = set(eq.varname for eq in self.equations.itervalues() if
                            eq.type in (DIFFERENTIAL_EQUATION,
                                           PARAMETER))
        arrays = {}
        for name in arrayvarnames:
            if isinstance(dtype, dict):
                curdtype = dtype[name]
            else:
                curdtype = dtype
            if curdtype is None:
                curdtype = brian_prefs['core.default_scalar_dtype']
            arrays[name] = DynamicArray1D(0)
        logger.debug("NeuronGroup memory allocated successfully.")
        return arrays

    def pre_run(self, namespace):
        # Replace dynamic arrays with arrays (necessary for C++ code)
        # The following is only a temporary hack, this doesn't allow to add
        # synapses between runs etc.
        for name in self.arrays:
            self.arrays[name] = self.arrays[name][:]
            self.specifiers[name].array = self.arrays[name]
        
        for prepost in self._indices:
            self._indices[prepost] = self._indices[prepost][:]
            self.specifiers['_presynaptic'].array = self._indices[prepost]
             

    def connect_one_to_one(self):
        ''' Manually create a one to one connectivity pattern '''

        if len(self.source) != len(self.target):
            raise TypeError('Can only create synapses between groups of same size')
        
        new_synapses = len(self.source)
        
        for array in self.arrays.itervalues():
            array.resize(new_synapses)
            
        for synapses in self._synapses.itervalues():            
            for i in xrange(new_synapses):                    
                synapses[i].resize(1)
                synapses[i][0] = i
        
        for indices in self._indices.itervalues():
            indices.resize(new_synapses)
            indices[:] = np.arange(new_synapses)
                
        for delays in self._delays.itervalues():
            delays.resize(new_synapses)

        self.N = new_synapses


def smallest_inttype(N):
    '''
    Returns the smallest signed integer dtype that can store N indexes.
    '''
    if N<=127:
        return np.int8
    elif N<=32727:
        return np.int16
    elif N<=2147483647:
        return np.int32
    else:
        return np.int64