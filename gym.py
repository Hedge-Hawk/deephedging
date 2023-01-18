"""
Deep Hedging Gym.
-----------------
Training environment for deep hedging.

June 30, 2022
@author: hansbuehler
"""
from .base import Logger, Config, tf, tfp, dh_dtype, pdct, tf_back_flatten, tf_make_dim, Int, Float, tfCast
from .agents import AgentFactory
from .objectives import MonetaryUtility
from collections.abc import Mapping
from cdxbasics.util import uniqueHash
import numpy as np

_log = Logger(__file__)

class VanillaDeepHedgingGym(tf.keras.Model):
    """ 
    Vanilla periodic policy search Deep Hedging engine https://arxiv.org/abs/1802.03042 
    Vewrsion 2.0 supports recursive and iterative networks
    Hans Buehler, June 2022
    """
    
    def __init__(self, config : Config, name : str = "VanillaDeepHedging", dtype = dh_dtype ):
        """
        Deep Hedging Gym.
        The design pattern here is that the gym instantiates the agent.
        This is done because the gym will know first the number of instruemnt.
        An alternative design would be to pass the agent as parameter but then
        validate that it has the correct number of instruments.
        
        Parameters 07ed683a03a89d54ff28a6385bfd0d48
        ----------
            config : Config
                Sets up the gym, and instantiates the agent
                Main config sections
                    agent     - will be passed to AgentFactory()
                    objective - will be passed to MonetaryUtility()
                    Print config.usage_report() after calling this object
                    for full help
            name : str
                Name of the object for progress mesages
            dtype : tf.DType
                Type
        """
        tf.keras.Model.__init__(self, name=name, dtype=dtype )
        seed                       = config.tensorflow("seed", 423423423, int, "Set tensor random seed. Leave to None if not desired.")
        self.hard_clip             = config.environment('hard_clip', False, bool, "Use min/max instread of soft clip for limiting actions by their bounds")
        self.outer_clip            = config.environment('outer_clip', True, bool, "Apply a hard clip 'outer_clip_cut_off' times the boundaries")
        self.outer_clip_cut_off    = config.environment('outer_clip_cut_off', 10., Float>=1., "Multiplier on bounds for outer_clip")
        hinge_softness             = config.environment('softclip_hinge_softness', 1., Float>0., "Specifies softness of bounding actions between lbnd_a and ubnd_a")
        self.softclip              = tfp.bijectors.SoftClip( low=0., high=1., hinge_softness=hinge_softness, name='soft_clip' )
        self.config_agent          = config.agent.detach()
        self.config_objective      = config.objective.detach()
        self.agent                 = None
        self.utility               = None
        self.utility0              = None
        self.unique_id             = config.unique_id()  # for serialization
        config.done()
        
        if not seed is None:
            tf.random.set_seed( seed )

    # -------------------
    # keras model pattern
    # -------------------
            
    def build(self, shapes : dict ):
        """ Build the model. See call(). """
        assert self.agent is None, "build() called twice?"
        _log.verify( isinstance(shapes, Mapping), "'shapes' must be a dictionary type. Found type %s", type(shapes ))

        nInst         = int( shapes['market']['hedges'][2] )
        self.agent    = AgentFactory( nInst, self.config_agent, name="agent", dtype=self.dtype ) 
        self.utility  = MonetaryUtility( self.config_objective, name="utility",  dtype=self.dtype ) 
        self.utility0 = MonetaryUtility( self.config_objective, name="utility0", dtype=self.dtype ) 

    def call( self, data : dict, training : bool = False ) -> dict:
        """
        Gym track.
        This function expects specific information in the dictionary data; see below
        
        Parameters
        ----------
            data : dict
                The data for the gym.
                It takes the following data with M=number of time steps, N=number of hedging instruments.
                First coordinate is number of samples in this batch.
                    market, hedges :            (,M,N) the returns of the hedges, per step, per instrument
                    market, cost:               (,M,N) proportional cost for trading, per step, per instrument
                    market, ubnd_a and lbnd_a : (,M,N) min max action, per step, per instrument
                    market, payoff:             (,M) terminal payoff of the underlying portfolio
                    
                    features, per_step:       (,M,N) list of features per step
                    features, per_sample:     (,M) list of features for each sample
                    
            training : bool, optional
                See tensorflow documentation
        
        Returns
        -------
            dict:
            This function returns analaytics of the performance of the agent
            on the path as a dictionary. Each is returned per sample
                utility:         (,) primary objective to maximize
                utility0:        (,) objective without hedging
                loss:            (,) -utility-utility0
                payoff:          (,) terminal payoff 
                pnl:             (,) mid-price pnl of trading (e.g. ex cost)
                cost:            (,) cost of trading
                gains:           (,) total gains: payoff + pnl - cost 
                actions:         (,M,N) actions, per step, per path
                deltas:          (,M,N) deltas, per step, per path
        """
        return self._call( tfCast(data), training )
    @tf.function  
    def _call( self, data : dict, training : bool ) -> dict:
        """ The _call function was introduced to allow conversion of numpy arrays into tensors ahead of tf.function tracing """
        _log.verify( isinstance(data, Mapping), "'data' must be a dictionary type. Found type %s", type(data ))
        assert not self.agent is None and not self.utility is None, "build() not called"
        
        # geometry
        # --------
        hedges       = data['market']['hedges']
        hedge_shape  = hedges.shape.as_list()
        _log.verify( len(hedge_shape) == 3, "data['market']['hedges']: expected tensor of dimension 3. Found shape %s", hedge_shape )
        nBatch       = hedge_shape[0]    # is None at first call. Later will be batch size
        nSteps       = hedge_shape[1]
        nInst        = hedge_shape[2]
        
        # extract market data
        # --------------------
        trading_cost = data['market']['cost']
        ubnd_a       = data['market']['ubnd_a']
        lbnd_a       = data['market']['lbnd_a']
        payoff       = data['market']['payoff']
        payoff       = payoff[:,0] if payoff.shape.as_list() == [nBatch,1] else payoff # handle tf<=2.6        
        _log.verify( trading_cost.shape.as_list() == [nBatch, nSteps, nInst], "data['market']['cost']: expected shape %s, found %s", [nBatch, nSteps, nInst], trading_cost.shape.as_list() )
        _log.verify( ubnd_a.shape.as_list() == [nBatch, nSteps, nInst], "data['market']['ubnd_a']: expected shape %s, found %s", [nBatch, nSteps, nInst], ubnd_a.shape.as_list() )
        _log.verify( lbnd_a.shape.as_list() == [nBatch, nSteps, nInst], "data['market']['lbnd_a']: expected shape %s, found %s", [nBatch, nSteps, nInst], lbnd_a.shape.as_list() )
        _log.verify( payoff.shape.as_list() == [nBatch], "data['market']['payoff']: expected shape %s, found %s", [nBatch], payoff.shape.as_list() )
        
        # features
        # --------
        features_per_step, \
        features_per_path = self._features( data, nSteps )
            
        # main loop
        # ---------

        pnl     = tf.zeros_like(payoff, dtype=dh_dtype)                                                       # [?,]
        cost    = tf.zeros_like(payoff, dtype=dh_dtype)                                                       # [?,]
        delta   = tf.zeros_like(trading_cost[:,0,:], dtype=dh_dtype)                                          # [?,nInst]
        action  = tf.zeros_like(trading_cost[:,0,:], dtype=dh_dtype)                                          # [?,nInst]
        actions = tf.zeros_like(trading_cost[:,0,:][:,tf.newaxis,:], dtype=dh_dtype)                          # [?,0,nInst]
        state   = self.agent.init_state({},training=training) if self.agent.is_recurrent else None
        state   = pnl[:,tf.newaxis]*0. + state[tf.newaxis,:] if not state is None else tf.zeros_like(pnl)     # [?,nStates] if states are used; [?] else
        
        t       = 0
        while tf.less(t,nSteps, name="main_loop"): # logically equivalent to: for t in range(nSteps):
            tf.autograph.experimental.set_loop_options( shape_invariants=[(actions, tf.TensorShape([None,None,nInst]))] )
            
            # build features, including recurrent state
            live_features = dict( action=action, delta=delta, cost=cost, pnl=pnl )
            live_features.update( { f:features_per_path[f] for f in features_per_path } )
            live_features.update( { f:features_per_step[f][:,t,:] for f in features_per_step})
            live_features['delta'] = delta
            live_features['action'] = action
            if self.agent.is_recurrent: live_features[ self.agent.state_feature_name ] = state

            # action
            action, state_ =  self.agent( live_features, training=training )
            _log.verify( action.shape.as_list() in [ [nBatch, nInst], [1, nInst] ], "Error: action: expected shape %s or %s, found %s", [nBatch, nInst], [1,nInst], action.shape.as_list() )
            action         =  action if len(action.shape) == 2 else action[tf.newaxis,:]
            action         =  self._clip_actions(action, lbnd_a[:,t,:], ubnd_a[:,t,:] )
            state          =  state_ if self.agent.is_recurrent else state
            delta          += action

            # record actions per path, per step
            action_        =  tf.stop_gradient( action )[:,tf.newaxis,:]
            actions        =  tf.concat( [actions,action_], axis=1, name="actions") if t>0 else action_
            
            # trade
            cost           += tf.reduce_sum( tf.math.abs( action ) * trading_cost[:,t,:], axis=1, name="cost_t" )
            pnl            += tf.reduce_sum( action * hedges[:,t,:], axis=1, name="pnl_t" )
            
            
            # iterate 
            t              += 1

        pnl  = tf.debugging.check_numerics(pnl, "Numerical error computing pnl in %s. Turn on tf.enable_check_numerics to find the root cause. Note that they are disabled in trainer.py" % __file__ )
        cost = tf.debugging.check_numerics(cost, "Numerical error computing cost in %s. Turn on tf.enable_check_numerics to find the root cause. Note that they are disabled in trainer.py" % __file__ )

        # compute utility
        # ---------------
        
        features_time_0 = {}
        features_time_0.update( { f:features_per_path[f] for f in features_per_path } )
        features_time_0.update( { f:features_per_step[f][:,0,:] for f in features_per_step})

        utility           = self.utility( data=dict(features_time_0 = features_time_0,
                                                    payoff          = payoff, 
                                                    pnl             = pnl,
                                                    cost            = cost ), training=training )
        utility0          = self.utility0(data=dict(features_time_0 = features_time_0,
                                                    payoff          = payoff, 
                                                    pnl             = pnl*0.,
                                                    cost            = cost*0.), training=training )
        # prepare output
        # --------------
            
        return pdct(
            loss     = -utility-utility0,                         # [?,]
            utility  = tf.stop_gradient( utility ),               # [?,]
            utility0 = tf.stop_gradient( utility0 ),              # [?,]
            gains    = tf.stop_gradient( payoff + pnl - cost ),   # [?,]
            payoff   = tf.stop_gradient( payoff ),                # [?,]
            pnl      = tf.stop_gradient( pnl ),                   # [?,]
            cost     = tf.stop_gradient( cost ),                  # [?,]
            actions  = tf.concat( actions, axis=1, name="actions" ) # [?,nSteps,nInst]
        )
        
    # -------------------
    # internal
    # -------------------

    def _clip_actions( self, actions, lbnd_a, ubnd_a ):
        """ Clip the action within lbnd_a, ubnd_a """
        
        with tf.control_dependencies( [ tf.debugging.assert_greater_equal( ubnd_a, lbnd_a, message="Upper bound for actions must be bigger than lower bound" ),
                                        tf.debugging.assert_greater_equal( ubnd_a, 0., message="Upper bound for actions must not be negative" ),
                                        tf.debugging.assert_less_equal( lbnd_a, 0., message="Lower bound for actions must not be positive" ) ] ):
        
            if self.hard_clip:
                # hard clip
                # this is recommended for debugging only.
                # soft clipping should lead to smoother gradients
                actions = tf.minimum( actions, ubnd_a, name="hard_clip_min" )
                actions = tf.maximum( actions, lbnd_a, name="hard_clip_max" )
                return actions            

            if self.outer_clip:
                # to avoid very numerical errors due to very
                # large pre-clip actions, we cap pre-clip values
                # hard at 10 times the bounds.
                # This can happen if an action has no effect
                # on the gains process (e.g. hedge == 0)
                actions = tf.minimum( actions, ubnd_a*self.outer_clip_cut_off, name="outer_clip_min" )
                actions = tf.maximum( actions, lbnd_a*self.outer_clip_cut_off, name="outer_clip_max" )

            dbnd = ubnd_a - lbnd_a
            rel  = ( actions - lbnd_a ) / dbnd
            rel  = self.softclip( rel )
            act  = tf.where( dbnd > 0., rel *  dbnd + lbnd_a, 0., name="soft_clipped_act" )
            act  = tf.debugging.check_numerics(act, "Numerical error clipping action in %s. Turn on tf.enable_check_numerics to find the root cause. Note that they are disabled in trainer.py" % __file__ )
            return act

    def _features( self, data : dict, nSteps : int) -> (dict, dict):
        """ 
        Collect requested features and convert them into common shapes.    
        
        Returns
        -------
            features_per_step, features_per_path : (dict, dict)
                features_per_step: requested features which are available per step. Each feature has dimension [nSamples,nSteps,M] for some M
                features_per_path: requested features with dimensions [nSamples,M]
        """
        features             = data.get('features',{})

        features_per_step_i  = features.get('per_step', {})
        features_per_step    = {}
        for f in features_per_step_i:
            feature = features_per_step_i[f]
            assert isinstance(feature, tf.Tensor), "Internal error: type %s found" % feature._class__.__name__
            _log.verify( len(feature.shape) >= 2, "data['features']['per_step']['%s']: expected tensor of at least dimension 2, found shape %s", f, feature.shape.as_list() )
            _log.verify( feature.shape[1] == nSteps, "data['features']['per_step']['%s']: second dimnsion must match number of steps, %ld, found shape %s", f, nSteps, feature.shape.as_list() )
            features_per_step[f] = tf_make_dim( feature, 3 )

        features_per_path_i    = features.get('per_path', {})
        features_per_path      = { tf_make_dim( _, dim=2 ) for _ in features_per_path_i }
        return features_per_step, features_per_path

    # -------------------
    # syntatic sugar
    # -------------------

    @property
    def num_trainable_weights(self) -> int:
        """ Returns the number of weights. The model must have been call()ed once """
        assert not self.agent is None, "build() must be called first"
        weights = self.trainable_weights
        return np.sum( [ np.prod( w.get_shape() ) for w in weights ] )
    
    @property
    def available_features_per_step(self) -> list:
        """ Returns the list of features available per time step (for the agent). The model must have been call()ed once """
        _log.verify( not self.agent is None, "Cannot call this function before model was built")
        return self.agent.available_features
    
    @property
    def available_features_per_path(self) -> list:
        """ Returns the list of features available per time step (for montetary utilities). The model must have been call()ed once """
        _log.verify( not self.utility is None, "Cannot call this function before model was built")
        return self.utility.available_features

    @property
    def agent_features_used(self) -> list:
        """ Returns the list of features used by the agent. The model must have been call()ed once """
        _log.verify( not self.agent is None, "Cannot call this function before model was built")
        return self.agent.public_features
    
    @property
    def utility_features_used(self) -> list:
        """ Returns the list of features available per time step (for the agent). The model must have been call()ed once """
        _log.verify( not self.agent is None, "Cannot call this function before model was built")
        return self.utility.features
    
    # -------------------
    # caching
    # -------------------
    
    def create_cache( self ):
        """
        Create a dictionary which allows reconstructing the current model.
        """
        assert not self.agent is None, "build() not called yet"
        opt_config  = tf.keras.optimizers.serialize( self.optimizer ) if not self.optimizer is None else None
        opt_weights = self.optimizer.get_weights() if not getattr(self.optimizer,"get_weights",None) is None else None        
        if not opt_config is None and opt_weights is None:
            # tensorflow 2.11 abandons 'get_weights'
            variables   = self.optimizer.variables()        
            opt_weights = [ np.array( v ) for v in variables ]
        
        return dict( gym_uid       = self.unique_id,
                     gym_weights   = self.get_weights(),
                     opt_uid       = uniqueHash( opt_config ) if not opt_config is None else "",
                     opt_config    = opt_config,
                     opt_weights   = self.optimizer.get_weights()
                   )
                
    def restore_from_cache( self, cache ) -> bool:
        """
        Restore 'self' from cache.
        Note that we have to call() this object before being able to use this function
        
        This function returns False if the cached weights do not match the current architecture.
        """        
        assert not self.agent is None, "build() not called yet"
        gym_uid     = cache['gym_uid']
        gym_weights = cache['gym_weights']
        opt_uid     = cache['opt_uid']
        opt_config  = cache['opt_config']
        opt_weights = cache['opt_weights']
        
        self_opt_config = tf.keras.optimizers.serialize( self.optimizer ) if not self.optimizer is None else None
        self_opt_uid    = uniqueHash( self_opt_config ) if not self_opt_config is None else ""
        
        # check that the objects correspond to the correct configs
        if gym_uid != self.unique_id:
            _log.warn( "Cache restoration error: provided cache object has gym ID %s vs current ID %s", gym_uid, self.unique_id)
            return False
        if opt_uid != self_opt_uid:
            _log.warn( "Cache restoration error: provided cache object has optimizer ID %s vs current ID %s\n"\
                       "Stored configuration: %s\nCurrent configuration: %s", opt_uid, self_opt_uid, opt_config, self_opt_config)
            return False

        # load weights
        # Note that we will continue with the restored weights for the gym even if we fail to restore the optimizer
        # This is likely the desired behaviour.
        try:
            self.set_weights( gym_weights )
        except ValueError as v:
            _log.warn( "Cache restoration error: provided cache gym weights were not compatible with the gym.\n%s", v)
            return False
        return True
    
        if self.optimizer is None:
            return True    
        try:
            self.optimizer.set_weights( opt_weights )
        except ValueError as v:
            isTF211 = getattr(self.optimizer,"get_weights",None) is None
            isTF211 = "" if not isTF211 else "Code is running TensorFlow 2.11 or higher for which tf.keras.optimizers.Optimizer.get_weights() was retired. Current code is experimental. Review create_cache/restore_from_cache.\n"
            _log.warn( "Cache restoration error: cached optimizer weights were not compatible with existing optimizer.\n%s%s", v)
            return False
        return True

