import tensorflow as tf
import numpy as np
import sys
import time
import math
import copy
import tf_loss
from tf_model_utils import get_reduce_loss_func, get_rnn_cell, linear, fully_connected_layer, get_activation_fn, get_decay_variable
from constants import Constants as C
from tf_rnn_cells import VRNNCell

"""
Vanilla variational recurrent neural network model.
The model is trained by using negative log-likelihood (reconstruction) and KL-divergence losses.
Assuming that model outputs are isotropic Gaussian distributions.

Model functionality is decomposed into basic functions (see build_graph method) so that variants of the model can easily
be constructed by inheriting from this vanilla architecture.

Note that different modes (i.e., training, validation, sampling) should be implemented as different graphs by reusing
the parameters. Therefore, validation functionality shouldn't be used by a model with training mode.
"""


class LatentLayer(object):
    """
    Base class for latent layers.
    """
    def __init__(self, config, mode, reuse, **kwargs):
        self.config = config
        self.reuse = reuse
        assert mode in [C.TRAIN, C.VALID, C.EVAL, C.SAMPLE]
        self.mode = mode
        self.is_sampling = mode == C.SAMPLE  # If it is a variational model, then prior distribution is used.
        self.is_validation = mode in [C.VALID, C.EVAL]
        self.is_training = mode == C.TRAIN
        self.is_eval = mode == C.EVAL  # Similar to the validation mode, returns some details for analysis.
        self.layer_structure = config.get("layer_structure")
        self.layer_fc = self.layer_structure == C.LAYER_FC
        self.layer_tcn = self.layer_structure == C.LAYER_TCN
        self.global_step = kwargs.get("global_step", None)

        self.ops_loss = dict()

    def build_latent_layer(self, q_input, p_input, output_ops_dict=None, eval_ops_dict=None, summary_ops_dict=None):
        """
        Given the inputs for approximate posterior and prior, builds corresponding latent distributions.
        Inserts latent ops into main model's containers. See BaseTemporalModel for details.
        Args:
            q_input: inputs for approximate posterior.
            p_input: inputs for prior.
            output_ops_dict:
            eval_ops_dict:
            summary_ops_dict
        Returns:
            A latent sample drawn from Q or P based on mode. In sampling mode, the sample is drawn from prior.
        """
        raise NotImplementedError('subclasses must override sample method')

    def build_loss(self, sequence_mask, reduce_loss_fn, loss_ops_dict, **kwargs):
        """
        Builds loss terms related with latent space.
        Args:
            sequence_mask: mask to be applied on variable-length sequences.
            reduce_loss_fn: function to get final loss value, i.e., average or sum.
            loss_ops_dict: container keeping loss terms.
        Returns:
            A dictionary of loss terms.
        """
        raise NotImplementedError('subclasses must override sample method')

    @staticmethod
    def build_tcn_layer(input_layer, num_latent_units, latent_activation_fn, kernel_size, dilation, num_hidden_layers, num_hidden_units, is_training):
        """
        Args:
            input_layer:
            num_latent_units:
            latent_activation_fn:
            kernel_size:
            dilation:
            num_hidden_layers:
            num_hidden_units:
            is_training:
        Returns:
        """
        # Whether to applies zero padding on the inputs or not. If kernel_size > 1 or dilation > 1, it needs to be True.
        zero_padding = True if kernel_size > 1 or dilation > 1 else False
        current_layer = [input_layer]
        for i in range(num_hidden_layers):
            current_layer = TCN.temporal_block(input_layer=current_layer[0], num_filters=num_hidden_units,
                                               kernel_size=kernel_size, dilation=dilation, activation_fn=None,
                                               use_gate=True, use_residual=False, zero_padding=zero_padding)

        current_layer = TCN.temporal_block(input_layer=current_layer[0], num_filters=num_latent_units,
                                           kernel_size=kernel_size, dilation=dilation, activation_fn=None,
                                           use_gate=True, use_residual=False, zero_padding=zero_padding)

        layer = current_layer[0] if latent_activation_fn is None else latent_activation_fn(current_layer[0])
        flat_layer = tf.reshape(layer, [-1, num_latent_units])
        return layer, flat_layer

    @staticmethod
    def build_conv1_layer(input_layer, num_latent_units, latent_activation_fn, num_hidden_layers, num_hidden_units, hidden_activation_fn, is_training):
        current_layer = input_layer
        for i in range(num_hidden_layers):
            current_layer = tf.layers.conv1d(inputs=current_layer, kernel_size=1, padding='valid',
                                             filters=num_hidden_units, dilation_rate=1,
                                             activation=hidden_activation_fn)

        current_layer = tf.layers.conv1d(inputs=current_layer, kernel_size=1, padding='valid',
                                         filters=num_latent_units, dilation_rate=1,
                                         activation=latent_activation_fn)

        flat_layer = tf.reshape(current_layer, [-1, num_latent_units])
        return current_layer, flat_layer

    @staticmethod
    def build_fc_layer(input_layer, num_latent_units, latent_activation_fn, num_hidden_layers, num_hidden_units, hidden_activation_fn, is_training):
        flat_input = tf.reshape(input_layer, [-1, input_layer.shape.as_list()[-1]])
        flat_hidden = fully_connected_layer(input_layer=flat_input,
                                            is_training=is_training,
                                            activation_fn=hidden_activation_fn,
                                            num_layers=num_hidden_layers,
                                            size=num_hidden_units)
        flat_layer = linear(input_layer=flat_hidden,
                            output_size=num_latent_units,
                            activation_fn=latent_activation_fn,
                            is_training=is_training)
        layer = tf.reshape(flat_layer, [tf.shape(input_layer)[0], -1, num_latent_units])
        return layer, flat_layer

    @staticmethod
    def get(layer_type, config, mode, reuse, **kwargs):
        """
        Creates latent layer.
        Args:
            layer_type (str): Type of layer.
            config:
            reuse:
            mode:
        Returns:
            An instance of LatentLayer.
        """
        if layer_type == C.LATENT_GAUSSIAN:
            return GaussianLatentLayer(config, mode, reuse, **kwargs)
        elif layer_type == C.LATENT_LADDER_GAUSSIAN:
            return LadderLatentLayer(config, mode, reuse, **kwargs)
        else:
            raise Exception("Unknown latent layer.")


class GaussianLatentLayer(LatentLayer):
    """
    VAE latent space for time-series data, modeled by a Gaussian distribution with diagonal covariance matrix.
    """
    def __init__(self, config, mode, reuse, **kwargs):
        super(GaussianLatentLayer, self).__init__(config, mode, reuse, )

        self.use_temporal_kld = self.config.get('use_temporal_kld', False)
        self.tkld_weight = self.config.get('tkld_weight', 0.1)
        self.kld_weight = self.config.get('kld_weight', 0.5)
        if not self.is_training:
            self.kld_weight = 1.0

        # Latent space components.
        self.p_mu = None
        self.q_mu = None
        self.p_sigma = None
        self.q_sigma = None

    def build_loss(self, sequence_mask, reduce_loss_fn, loss_ops_dict, **kwargs):
        """
        Creates KL-divergence loss between prior and approximate posterior distributions. If use_temporal_kld is True,
        then creates another KL-divergence term between consecutive approximate posteriors in time.
        """
        loss_key = "loss_kld"
        with tf.name_scope("kld_loss"):
            self.ops_loss[loss_key] = self.kld_weight*reduce_loss_fn(
                sequence_mask*tf_loss.kld_normal_isotropic(self.q_mu,
                                                           self.q_sigma,
                                                           self.p_mu,
                                                           self.p_sigma,
                                                           reduce_sum=False))
            loss_ops_dict[loss_key] = self.ops_loss[loss_key]

        if self.is_training and self.use_temporal_kld:
            prior_step = 1
            latent_shape = tf.shape(self.q_sigma)

            p_mu_part = tf.zeros([latent_shape[0], prior_step, latent_shape[2]], name="p_mu")
            p_sigma_part = tf.ones([latent_shape[0], prior_step, latent_shape[2]], name="p_sigma")
            q_mu_part = self.q_mu[:, 0:-prior_step, :]
            q_sigma_part = self.q_sigma[:, 0:-prior_step, :]
            temp_p_mu = tf.concat([p_mu_part, q_mu_part], axis=1)
            temp_p_sigma = tf.concat([p_sigma_part, q_sigma_part], axis=1)

            loss_key = "loss_temporal_kld"
            with tf.name_scope("temporal_kld_loss"):
                self.ops_loss[loss_key] = self.tkld_weight*reduce_loss_fn(
                    sequence_mask*tf_loss.kld_normal_isotropic(self.q_mu,
                                                               self.q_sigma,
                                                               tf.stop_gradient(temp_p_mu),
                                                               tf.stop_gradient(temp_p_sigma),
                                                               reduce_sum=False))
                loss_ops_dict[loss_key] = self.ops_loss[loss_key]
        return self.ops_loss

    def build_latent_layer(self, q_input, p_input, output_ops_dict=None, eval_ops_dict=None, summary_ops_dict=None):
        """
        Prior distribution is estimated by using information until the current time-step t. On the other hand,
        approximate-posterior distribution is estimated by using some future steps.

        Note that zero_padding is not applied for approximate posterior (i.e., q). In order to have the same length
        outputs with inputs, zero padding should be applied on q_input beforehand.
        """
        with tf.variable_scope('prior', reuse=self.reuse):
            with tf.variable_scope('p_mu', reuse=self.reuse):
                if self.layer_tcn:
                    self.p_mu, _ = LatentLayer.build_tcn_layer(input_layer=p_input,
                                                               num_latent_units=self.config['latent_size'],
                                                               latent_activation_fn=None,
                                                               kernel_size=self.config['latent_filter_size'],
                                                               dilation=self.config['latent_dilation'],
                                                               num_hidden_layers=self.config["num_hidden_layers"],
                                                               num_hidden_units=self.config["num_hidden_units"],
                                                               is_training=self.is_training)
                elif self.layer_fc:
                    self.p_mu, _ = LatentLayer.build_fc_layer(input_layer=p_input,
                                                              num_latent_units=self.config['latent_size'],
                                                              latent_activation_fn=None,
                                                              num_hidden_layers=self.config["num_hidden_layers"],
                                                              num_hidden_units=self.config["num_hidden_units"],
                                                              hidden_activation_fn=self.config["hidden_activation_fn"],
                                                              is_training=self.is_training)
            with tf.variable_scope('p_sigma', reuse=self.reuse):
                if self.layer_tcn:
                    self.p_sigma, _ = LatentLayer.build_tcn_layer(input_layer=p_input,
                                                                  num_latent_units=self.config['latent_size'],
                                                                  latent_activation_fn=tf.exp,
                                                                  kernel_size=self.config['latent_filter_size'],
                                                                  dilation=self.config['latent_dilation'],
                                                                  num_hidden_layers=self.config["num_hidden_layers"],
                                                                  num_hidden_units=self.config["num_hidden_units"],
                                                                  is_training=self.is_training)
                elif self.layer_fc:
                    self.p_sigma, _ = LatentLayer.build_fc_layer(input_layer=p_input,
                                                                 num_latent_units=self.config['latent_size'],
                                                                 latent_activation_fn=tf.exp,
                                                                 num_hidden_layers=self.config["num_hidden_layers"],
                                                                 num_hidden_units=self.config["num_hidden_units"],
                                                                 hidden_activation_fn=self.config["hidden_activation_fn"],
                                                                 is_training=self.is_training)
                if self.config.get('latent_sigma_threshold', 0) > 0:
                    self.p_sigma = tf.clip_by_value(self.p_sigma, 1e-3, self.config.get('latent_sigma_threshold'))

        with tf.variable_scope('approximate_posterior', reuse=self.reuse):
            with tf.variable_scope('q_mu', reuse=self.reuse):
                if self.layer_tcn:
                    self.q_mu, _ = LatentLayer.build_tcn_layer(input_layer=q_input,
                                                               num_latent_units=self.config['latent_size'],
                                                               latent_activation_fn=None,
                                                               kernel_size=self.config['latent_filter_size'],
                                                               dilation=self.config['latent_dilation'],
                                                               num_hidden_layers=self.config["num_hidden_layers"],
                                                               num_hidden_units=self.config["num_hidden_units"],
                                                               is_training=self.is_training)
                elif self.layer_fc:
                    self.q_mu, _ = LatentLayer.build_fc_layer(input_layer=q_input,
                                                              num_latent_units=self.config['latent_size'],
                                                              latent_activation_fn=None,
                                                              num_hidden_layers=self.config["num_hidden_layers"],
                                                              num_hidden_units=self.config["num_hidden_units"],
                                                              hidden_activation_fn=self.config["hidden_activation_fn"],
                                                              is_training=self.is_training)
            with tf.variable_scope('q_sigma', reuse=self.reuse):
                if self.layer_tcn:
                    self.q_sigma, _ = LatentLayer.build_tcn_layer(input_layer=q_input,
                                                                  num_latent_units=self.config['latent_size'],
                                                                  latent_activation_fn=tf.exp,
                                                                  kernel_size=self.config['latent_filter_size'],
                                                                  dilation=self.config['latent_dilation'],
                                                                  num_hidden_layers=self.config["num_hidden_layers"],
                                                                  num_hidden_units=self.config["num_hidden_units"],
                                                                  is_training=self.is_training)
                elif self.layer_fc:
                    self.q_sigma, _ = LatentLayer.build_fc_layer(input_layer=q_input,
                                                                 num_latent_units=self.config['latent_size'],
                                                                 latent_activation_fn=tf.exp,
                                                                 num_hidden_layers=self.config["num_hidden_layers"],
                                                                 num_hidden_units=self.config["num_hidden_units"],
                                                                 hidden_activation_fn=self.config["hidden_activation_fn"],
                                                                 is_training=self.is_training)
                if self.config.get('latent_sigma_threshold', 0) > 0:
                    self.q_sigma = tf.clip_by_value(self.q_sigma, 1e-3, self.config.get('latent_sigma_threshold'))

        with tf.variable_scope('z', reuse=self.reuse):
            if self.is_sampling:
                eps = tf.random_normal(tf.shape(self.p_sigma), 0.0, 1.0, dtype=tf.float32)
                p_z = tf.add(self.p_mu, tf.multiply(self.p_sigma, eps))
                latent_sample = p_z
            else:
                eps = tf.random_normal(tf.shape(self.q_sigma), 0.0, 1.0, dtype=tf.float32)
                q_z = tf.add(self.q_mu, tf.multiply(self.q_sigma, eps))
                latent_sample = q_z

        # Register latent ops and summaries.
        if output_ops_dict is not None:
            output_ops_dict[C.P_MU] = self.p_mu
            output_ops_dict[C.P_SIGMA] = self.p_sigma
            output_ops_dict[C.Q_MU] = self.q_mu
            output_ops_dict[C.Q_SIGMA] = self.q_sigma
        if eval_ops_dict is not None:
            eval_ops_dict[C.P_MU] = self.p_mu
            eval_ops_dict[C.P_SIGMA] = self.p_sigma
            if not self.is_sampling:
                eval_ops_dict[C.Q_MU] = self.q_mu
                eval_ops_dict[C.Q_SIGMA] = self.q_sigma
        if summary_ops_dict is not None:
            summary_ops_dict["mean_" + C.P_MU] = tf.reduce_mean(self.p_mu)
            summary_ops_dict["mean_" + C.P_SIGMA] = tf.reduce_mean(self.p_sigma)
            summary_ops_dict["mean_" + C.Q_MU] = tf.reduce_mean(self.q_mu)
            summary_ops_dict["mean_" + C.Q_SIGMA] = tf.reduce_mean(self.q_sigma)

        return latent_sample


class LadderLatentLayer(LatentLayer):
    """
    Ladder VAE latent space for time-series data where each step is modeled by a Gaussian distribution with diagonal
    covariance matrix.
    """
    def __init__(self, config, mode, reuse, **kwargs):
        super(LadderLatentLayer, self).__init__(config, mode, reuse, **kwargs)

        # STCN-dense configuration. Concatenates the samples drawn from all latent variables.
        self.dense_z = self.config.get('dense_z', False)
        # Determines the number of deterministic layers per latent variable.
        self.vertical_dilation = self.config.get('vertical_dilation', 1)
        # Draw a new sample from the approximated posterior whenever needed. Otherwise, draw once and use it every time.
        self.use_same_q_sample = self.config.get('use_same_q_sample', False)
        # Whether the top-most prior is dynamic or not. LadderVAE paper uses standard N(0,I) prior.
        self.use_fixed_pz1 = self.config.get('use_fixed_pz1', False)
        # Prior is calculated by using the deterministic representations at previous step.
        self.dynamic_prior = self.config.get('dynamic_prior', False)
        # Approximate posterior is estimated as a precision weighted update of the prior and initial model predictions.
        self.precision_weighted_update = self.config.get('precision_weighted_update', True)
        # Whether the q distribution is hierarchically updated as in the case of prior or not. In other words, lower
        # q layer uses samples of the upper q layer.
        self.recursive_q = self.config.get('recursive_q', True)
        # Whether we follow top-down or bottom-up hierarchy.
        self.top_down_latents = self.config.get('top_down_latents', True)
        # Network type (i.e., dense, convolutional, etc.) we use to parametrize the latent distributions.
        self.latent_layer_structure = self.config.get('layer_structure', C.LAYER_CONV1)

        # Annealing KL-divergence weight or using fixed weight.
        kld_weight = self.config.get('kld_weight', 1)
        if isinstance(kld_weight, dict) and self.global_step:
            self.kld_weight = get_decay_variable(global_step=self.global_step, config=kld_weight, name="kld_weight")
        else:
            self.kld_weight = kld_weight

        # It is always 1 when we report the loss.
        if not self.is_training:
            self.kld_weight = 1.0

        # Latent space components.
        self.p_mu = []
        self.q_mu = []
        self.p_sigma = []
        self.q_sigma = []

        self.num_d_layers = None  # Total number of deterministic layers.
        self.num_s_layers = None  # Total number of stochastic layers can be different due to the vertical_dilation.
        self.q_approximate = None  # List of approximate q distributions from the recognition network.
        self.q_dists = None  # List of q distributions after updating with p_dists.
        self.p_dists = None  # List of prior distributions.
        self.kld_loss_terms = []  # List of KLD loss term.
        self.latent_samples = []  # List of latent samples.

    def build_latent_dist_conv1(self, input_, idx, scope, reuse):
        with tf.name_scope(scope):
            with tf.variable_scope(scope+'_mu', reuse=reuse):
                mu, flat_mu = LatentLayer.build_conv1_layer(input_layer=input_,
                                                            num_latent_units=self.config['latent_size'][idx],
                                                            latent_activation_fn=None,
                                                            num_hidden_layers=self.config["num_hidden_layers"],
                                                            num_hidden_units=self.config["num_hidden_units"],
                                                            hidden_activation_fn=self.config["hidden_activation_fn"],
                                                            is_training=self.is_training)
            with tf.variable_scope(scope+'_sigma', reuse=reuse):
                sigma, flat_sigma = LatentLayer.build_conv1_layer(input_layer=input_,
                                                                  num_latent_units=self.config['latent_size'][idx],
                                                                  latent_activation_fn=tf.nn.softplus,
                                                                  num_hidden_layers=self.config["num_hidden_layers"],
                                                                  num_hidden_units=self.config["num_hidden_units"],
                                                                  hidden_activation_fn=self.config["hidden_activation_fn"],
                                                                  is_training=self.is_training)
                if self.config.get('latent_sigma_threshold', 0) > 0:
                    sigma = tf.clip_by_value(sigma, 1e-3, self.config.get('latent_sigma_threshold'))
                    flat_sigma = tf.clip_by_value(flat_sigma, 1e-3, self.config.get('latent_sigma_threshold'))

        return (mu, sigma),  (flat_mu, flat_sigma)

    def build_latent_dist_tcn(self, input_, idx, scope, reuse):
        with tf.name_scope(scope):
            with tf.variable_scope(scope + '_mu', reuse=reuse):
                mu, flat_mu = LatentLayer.build_tcn_layer(input_layer=input_,
                                                          num_latent_units=self.config['latent_size'][idx],
                                                          latent_activation_fn=None,
                                                          kernel_size=self.config.get("kernel_size", 1),
                                                          dilation=self.config.get("dilation", 1),
                                                          num_hidden_layers=self.config["num_hidden_layers"],
                                                          num_hidden_units=self.config["num_hidden_units"],
                                                          is_training=self.is_training)
            with tf.variable_scope(scope + '_sigma', reuse=reuse):
                sigma, flat_sigma = LatentLayer.build_tcn_layer(input_layer=input_,
                                                                num_latent_units=self.config['latent_size'][idx],
                                                                latent_activation_fn=tf.nn.softplus,
                                                                kernel_size=self.config.get("kernel_size", 1),
                                                                dilation=self.config.get("dilation", 1),
                                                                num_hidden_layers=self.config["num_hidden_layers"],
                                                                num_hidden_units=self.config["num_hidden_units"],
                                                                is_training=self.is_training)
                if self.config.get('latent_sigma_threshold', 0) > 0:
                    sigma = tf.clip_by_value(sigma, 1e-3, self.config.get('latent_sigma_threshold'))
                    flat_sigma = tf.clip_by_value(flat_sigma, 1e-3, self.config.get('latent_sigma_threshold'))

        return (mu, sigma), (flat_mu, flat_sigma)

    def build_latent_dist_fc(self, input_, idx, scope, reuse):
        with tf.name_scope(scope):
            with tf.variable_scope(scope+'_mu', reuse=reuse):
                mu, flat_mu = LatentLayer.build_fc_layer(input_layer=input_,
                                                         num_latent_units=self.config['latent_size'][idx],
                                                         latent_activation_fn=None,
                                                         num_hidden_layers=self.config["num_hidden_layers"],
                                                         num_hidden_units=self.config["num_hidden_units"],
                                                         hidden_activation_fn=self.config["hidden_activation_fn"],
                                                         is_training=self.is_training)

            with tf.variable_scope(scope+'_sigma', reuse=reuse):
                sigma, flat_sigma = LatentLayer.build_fc_layer(input_layer=input_,
                                                               num_latent_units=self.config['latent_size'][idx],
                                                               latent_activation_fn=tf.nn.softplus,
                                                               num_hidden_layers=self.config["num_hidden_layers"],
                                                               num_hidden_units=self.config["num_hidden_units"],
                                                               hidden_activation_fn=self.config["hidden_activation_fn"],
                                                               is_training=self.is_training)
                if self.config.get('latent_sigma_threshold', 0) > 0:
                    sigma = tf.clip_by_value(sigma, 1e-3, self.config.get('latent_sigma_threshold'))
                    flat_sigma = tf.clip_by_value(flat_sigma, 1e-3, self.config.get('latent_sigma_threshold'))

        return (mu, sigma),  (flat_mu, flat_sigma)

    def build_latent_dist(self, input_, idx, scope, reuse):
        """
        Given the input parametrizes a Normal distribution.
        Args:
            input_:
            idx:
            scope: "approximate_posterior" or "prior".
            reuse:
        Returns:
            mu and sigma tensors.
        """
        if self.latent_layer_structure == C.LAYER_FC:
            return self.build_latent_dist_fc(input_, idx, scope, reuse)
        elif self.latent_layer_structure == C.LAYER_TCN:
            return self.build_latent_dist_tcn(input_, idx, scope, reuse)
        elif self.latent_layer_structure == C.LAYER_CONV1:
            return self.build_latent_dist_conv1(input_, idx, scope, reuse)
        else:
            raise Exception("Unknown latent layer type.")

    def build_latent_layer(self, q_input, p_input, output_ops_dict=None, eval_ops_dict=None, summary_ops_dict=None):
        """
        Builds stochastic latent variables hierarchically. q_input and p_input consist of outputs of stacked
        deterministic layers. self.vertical_dilation hyper-parameter denotes the size of the deterministic block. For
        example, if it is 5, then every fifth deterministic layer is used to estimate a random variable.

        Args:
            q_input (list): deterministic units to estimate the approximate posterior.
            p_input (list): deterministic units to estimate the prior.
            output_ops_dict (dict):
            eval_ops_dict (dict):
            summary_ops_dict (dict):

        Returns:
            A latent sample.
        """
        p_scope, q_scope = C.LATENT_P, C.LATENT_Q

        self.num_d_layers = len(q_input)
        assert self.num_d_layers % self.vertical_dilation == 0, "# of deterministic layers must be divisible by vertical dilation."
        self.num_s_layers = int(self.num_d_layers / self.vertical_dilation)

        # TODO
        self.config['latent_size'] = self.config['latent_size'] if isinstance(self.config['latent_size'], list) else [self.config['latent_size']]*self.num_s_layers

        self.q_approximate = [0]*self.num_s_layers
        self.q_dists = [0]*self.num_s_layers
        self.p_dists = [0]*self.num_s_layers

        # Indexing latent variables.
        if self.top_down_latents:
            # Build the top most latent layer.
            sl = self.num_s_layers-1  # stochastic layer index.
        else:
            sl = 0  # stochastic layer index.
        dl = (sl + 1)*self.vertical_dilation - 1  # deterministic layer index.

        # Estimate the prior of the first stochastic layer.
        scope = p_scope + "_" + str(sl + 1)
        reuse = self.reuse
        if self.dynamic_prior:
            if self.use_fixed_pz1:
                with tf.name_scope(scope):
                    latent_size = self.config['latent_size'][sl]
                    prior_shape = (tf.shape(p_input[0])[0], tf.shape(p_input[0])[1], latent_size)
                    p_dist = (tf.zeros(prior_shape, dtype=tf.float32), tf.ones(prior_shape, dtype=tf.float32))
            else:
                p_layer_inputs = [p_input[dl]]
                p_dist, _ = self.build_latent_dist(tf.concat(p_layer_inputs, axis=-1), idx=sl, scope=scope, reuse=reuse)
        else:
            # Insert N(0,1) as prior.
            with tf.name_scope(scope):
                latent_size = self.config['latent_size'][sl]
                prior_shape = (tf.shape(p_input[0])[0], tf.shape(p_input[0])[1], latent_size)
                p_dist = (tf.zeros(prior_shape, dtype=tf.float32), tf.ones(prior_shape, dtype=tf.float32))

        self.p_dists[sl] = p_dist
        # If it is not training, then we draw latent samples from the prior distribution.
        if self.is_sampling and self.dynamic_prior:
            posterior = p_dist
        else:
            scope = q_scope + "_" + str(sl + 1)
            reuse = self.reuse

            q_layer_inputs = [q_input[dl]]
            q_dist_approx, q_dist_approx_flat = self.build_latent_dist(tf.concat(q_layer_inputs, axis=-1), idx=sl, scope=scope, reuse=reuse)
            self.q_approximate[sl] = q_dist_approx

            # Estimate the approximate posterior distribution as a precision-weighted combination.
            if self.precision_weighted_update:
                scope = q_scope + "_pwu_" + str(sl + 1)
                q_dist = self.combine_normal_dist(q_dist_approx, p_dist, scope=scope)
            else:
                q_dist = q_dist_approx
            self.q_dists[sl] = q_dist
            # Set the posterior.
            posterior = q_dist

        posterior_sample_scope = "app_posterior_" + str(sl+1)
        posterior_sample = self.draw_latent_sample(posterior[0], posterior[1], p_dist[0], p_dist[1], scope=posterior_sample_scope, idx=sl)
        if self.dense_z:
            self.latent_samples.append(posterior_sample)

        # Build hierarchy.
        if self.top_down_latents:
            loop_indices = range(self.num_s_layers-2, -1, -1)
        else:
            loop_indices = range(1, self.num_s_layers, 1)
        for sl in loop_indices:
            dl = (sl + 1)*self.vertical_dilation - 1

            p_dist_preceding = p_dist
            # Estimate the prior distribution.
            scope = p_scope + "_" + str(sl + 1)
            reuse = self.reuse

            # Draw a latent sample from the preceding posterior.
            if not self.use_same_q_sample:
                posterior_sample = self.draw_latent_sample(posterior[0], posterior[1], p_dist[0], p_dist[1], posterior_sample_scope, sl)

            if self.dynamic_prior:  # Concatenate TCN representation with a sample from the approximated posterior.
                p_layer_inputs = [p_input[dl], posterior_sample]
            else:
                p_layer_inputs = [posterior_sample]

            p_dist, p_dist_flat = self.build_latent_dist(tf.concat(p_layer_inputs, axis=-1), idx=sl, scope=scope, reuse=reuse)
            self.p_dists[sl] = p_dist

            if self.is_sampling and self.dynamic_prior:
                # Set the posterior.
                posterior = p_dist
            else:
                # Estimate the uncorrected approximate posterior distribution.
                scope = q_scope + "_" + str(sl + 1)
                reuse = self.reuse

                q_layer_inputs = [q_input[dl]]
                if self.recursive_q:
                    # Draw a latent sample from the preceding posterior.
                    if not self.use_same_q_sample:
                        posterior_sample = self.draw_latent_sample(posterior[0], posterior[1], p_dist_preceding[0], p_dist_preceding[1], posterior_sample_scope, sl)
                    q_layer_inputs.append(posterior_sample)

                q_dist_approx, q_dist_approx_flat = self.build_latent_dist(tf.concat(q_layer_inputs, axis=-1), idx=sl, scope=scope, reuse=reuse)
                self.q_approximate[sl] = q_dist_approx

                # Estimate the approximate posterior distribution as a precision-weighted combination.
                if self.precision_weighted_update:
                    scope = q_scope + "_pwu_" + str(sl + 1)
                    q_dist = self.combine_normal_dist(q_dist_approx, p_dist, scope=scope)
                else:
                    q_dist = q_dist_approx
                self.q_dists[sl] = q_dist
                # Set the posterior.
                posterior = q_dist

            # Draw a new sample from the approximated posterior distribution of this layer.
            posterior_sample_scope = "app_posterior_" + str(sl+1)
            posterior_sample = self.draw_latent_sample(posterior[0], posterior[1], p_dist[0], p_dist[1], posterior_sample_scope, sl)
            if self.dense_z:
                self.latent_samples.append(posterior_sample)

        # TODO Missing an activation function. Do we need one here?
        if self.dense_z:  # Concatenate the latent samples of all stochastic layers.
            return tf.concat(self.latent_samples, axis=-1)
        else:  # Use a latent sample from the final stochastic layer.
            return self.draw_latent_sample(posterior[0], posterior[1], p_dist[0], p_dist[1], posterior_sample_scope, sl)

    def build_loss(self, sequence_mask, reduce_loss_fn, loss_ops_dict, **kwargs):
        """
        Creates KL-divergence loss between prior and approximate posterior distributions. If use_temporal_kld is True,
        then creates another KL-divergence term between consecutive approximate posteriors in time.
        """
        # eval_dict contains each KLD term and latent q, p distributions for further analysis.
        eval_dict = kwargs.get("eval_dict", None)
        if eval_dict is not None:
            eval_dict["q_dists"] = self.q_dists
            eval_dict["p_dists"] = self.p_dists
        if not self.is_sampling:
            loss_key = "loss_kld"
            kld_loss = 0.0
            with tf.name_scope("kld_loss"):
                for sl in range(self.num_s_layers-1, -1, -1):
                    with tf.name_scope("kld_" + str(sl)):
                        seq_kld_loss = sequence_mask*tf_loss.kld_normal_isotropic(self.q_dists[sl][0],
                                                                                  self.q_dists[sl][1],
                                                                                  self.p_dists[sl][0],
                                                                                  self.p_dists[sl][1],
                                                                                  reduce_sum=False)
                        kld_term = self.kld_weight*reduce_loss_fn(seq_kld_loss)

                        # This is just for monitoring. Only the entries in loss_ops_dict starting with "loss"
                        # contribute to the gradients.
                        if not self.is_training:
                            loss_ops_dict["KL"+str(sl)] = tf.stop_gradient(kld_term)

                        self.kld_loss_terms.append(kld_term)
                        kld_loss += kld_term
                        if eval_dict is not None:
                            eval_dict["summary_kld_" + str(sl)] = kld_term
                            eval_dict["sequence_kld_" + str(sl)] = seq_kld_loss

                # Optimization is done through the accumulated term (i.e., loss_ops_dict[loss_key]).
                self.ops_loss[loss_key] = kld_loss
                loss_ops_dict[loss_key] = kld_loss

    @classmethod
    def draw_latent_sample(cls, posterior_mu, posterior_sigma, prior_mu, prior_sigma, scope, idx):
        """
        Draws a latent sample by using the reparameterization trick.
        Args:
            prior_mu:
            prior_sigma:
            posterior_mu:
            posterior_sigma:
            scope:
            idx:
        Returns:
        """
        def normal_sample(mu, sigma):
            eps = tf.random_normal(tf.shape(sigma), 0.0, 1.0, dtype=tf.float32)
            return tf.add(mu, tf.multiply(sigma, eps))

        with tf.name_scope(scope+"_z"):
            z = normal_sample(posterior_mu, posterior_sigma)
        return z

    @classmethod
    def combine_normal_dist(cls, dist1, dist2, scope):
        """
        Calculates precision-weighted combination of two Normal distributions.
        Args:
            dist1: (mu, sigma)
            dist2: (mu, sigma)
            scope:
        Returns:
        """
        with tf.name_scope(scope):
            mu1, mu2 = dist1[0], dist2[0]
            precision1, precision2 = tf.pow(dist1[1], -2), tf.pow(dist2[1], -2)

            sigma = 1.0/(precision1 + precision2)
            mu = (mu1*precision1 + mu2*precision2) / (precision1 + precision2)

            return mu, sigma


class BaseTemporalModel(object):
    """
    Model class for modeling of temporal data, providing auxiliary functions implementing tensorflow routines and
    abstract functions to build model.
    """
    def __init__(self, config, session, reuse, mode, placeholders, input_dims, target_dims, **kwargs):
        self.config = config
        self.global_step = kwargs.get("global_step", None)

        # "sampling" is only valid for generative models.
        assert mode in [C.TRAIN, C.VALID, C.EVAL, C.SAMPLE]
        self.mode = mode
        self.is_sampling = mode == C.SAMPLE  # If it is a variational model, then prior distribution is used.
        self.is_validation = mode in [C.VALID, C.EVAL]
        self.is_training = mode == C.TRAIN
        self.is_eval = mode == C.EVAL  # Similar to the validation mode, returns some details for analysis.
        self.print_every_step = self.config.get('print_every_step')

        self.reuse = reuse
        self.session = session

        self.placeholders = placeholders
        self.pl_inputs = placeholders[C.PL_INPUT]
        self.pl_targets = placeholders[C.PL_TARGET]
        self.pl_seq_length = placeholders[C.PL_SEQ_LEN]
        self.seq_loss_mask = tf.expand_dims(tf.sequence_mask(lengths=self.pl_seq_length, dtype=tf.float32), -1)

        # Create an activation function for std predictions.
        sigma_threshold = config.get("sigma_threshold", 50.0)
        self.sigma_activation_fn = lambda x: tf.clip_by_value(tf.nn.softplus(x), 1e-3, sigma_threshold)

        # Creates a sample by using model outputs.
        self.sample_fn_tf, self.sample_fn_np = config.get_sample_function()

        self.input_dims = input_dims.copy()
        self.target_dims = target_dims.copy()
        self.target_pieces = tf.split(self.pl_targets, target_dims, axis=2)

        input_shape = self.pl_inputs.shape.as_list()  # Check if input shape is defined.
        self.batch_size = tf.shape(self.pl_inputs)[0] if input_shape[0] is None else input_shape[0]
        self.sequence_length = tf.shape(self.pl_inputs)[1] if input_shape[1] is None else input_shape[1]

        self.output_layer_config = copy.deepcopy(config.get('output_layer'))
        # Update output ops.
        self.loss_config = copy.deepcopy(self.config.get('loss', None))

        # Function to calculate final loss value (i.e., average or sum). See get_reduce_loss_func in tf_model_utils.py
        self.reduce_loss_fn = None
        # Loss op to be used during training.
        self.loss = None
        # Tensorflow summary object for loss plots.
        self.loss_summary = None
        # Accumulating likelihood loss terms.
        self.likelihood = 0

        # Model's output.
        self.output_sample = None
        # Model's raw input
        self.input_sample = None
        # Output of initial input layer.
        self.inputs_hidden = None

        # In validation/evaluation mode we first accumulate losses and then plot.
        # At the end of validation loop, we calculate average performance on the whole validation dataset and create
        # corresponding summary entries. See build_summary_plots method and `Summary methods for validation mode`
        # section.
        # Create containers and placeholders for every loss term. After each validation step, keep adding losses.
        if not self.is_training:
            self.container_loss = dict()
            self.container_loss_placeholders = dict()
            self.container_loss_summaries = dict()
            self.container_validation_feed_dict = dict()
            self.validation_summary_num_runs = 0

        # Ops to be evaluated by training loop function. It is a dictionary containing <key, value> pairs where the
        # `value` is tensorflow graph op. For example, summary, loss, training operations. Note that different modes
        # (i.e., training, sampling, validation) may have different set of ops.
        self.ops_run_loop = dict()
        # `summary` ops are kept in a list.
        self.ops_run_loop['summary'] = []

        # Dictionary of model outputs such as logits or mean and sigma of Gaussian distribution modeling outputs.
        # They are used in making predictions and creating loss terms.
        self.ops_model_output = dict()

        # To keep track of loss ops. List of loss terms that must be evaluated by session.run during training.
        self.ops_loss = dict()

        # (Default) graph ops to be fed into session.run while evaluating the model. Note that tf_evaluate* codes expect
        # to get these op results.
        self.ops_evaluation = dict()

        # Graph ops for scalar summaries such as average predicted variance.
        self.ops_scalar_summary = dict()

        # Auxiliary ops to be used in analysis of the model. It is used only in the evaluation mode.
        self.ops_for_eval_mode = dict()

        # Total number of trainable parameters.
        self.num_parameters = None

        for loss_name, loss_entry in self.loss_config.items():
            self.define_loss(loss_entry)

    def define_loss(self, loss_config):
        if loss_config['type'] in [C.NLL_NORMAL, C.NLL_BINORMAL]:
            self.output_layer_config['out_keys'].append(loss_config['out_key'] + C.SUF_MU)
            self.output_layer_config['out_dims'].append(self.target_dims[loss_config['target_idx']])
            self.output_layer_config['out_activation_fn'].append(None)

            self.output_layer_config['out_keys'].append(loss_config['out_key'] + C.SUF_SIGMA)
            self.output_layer_config['out_dims'].append(self.target_dims[loss_config['target_idx']])
            self.output_layer_config['out_activation_fn'].append(self.sigma_activation_fn)

        if loss_config['type'] in [C.NLL_BINORMAL]:
            self.output_layer_config['out_keys'].append(loss_config['out_key']+C.SUF_RHO)
            self.output_layer_config['out_dims'].append(1)
            self.output_layer_config['out_activation_fn'].append(C.TANH)

        if loss_config['type'] in [C.NLL_GMM, C.NLL_BIGMM]:
            self.output_layer_config['out_keys'].append(loss_config['out_key'] + C.SUF_MU)
            self.output_layer_config['out_dims'].append(self.target_dims[loss_config['target_idx']] * loss_config['num_components'])
            self.output_layer_config['out_activation_fn'].append(None)

            self.output_layer_config['out_keys'].append(loss_config['out_key'] + C.SUF_SIGMA)
            self.output_layer_config['out_dims'].append(self.target_dims[loss_config['target_idx']] * loss_config['num_components'])
            self.output_layer_config['out_activation_fn'].append(self.sigma_activation_fn)

            self.output_layer_config['out_keys'].append(loss_config['out_key']+C.SUF_COEFFICIENT)
            self.output_layer_config['out_dims'].append(loss_config['num_components'])
            self.output_layer_config['out_activation_fn'].append(C.SOFTMAX)

        if loss_config['type'] == C.NLL_BIGMM:
            self.output_layer_config['out_keys'].append(loss_config['out_key']+C.SUF_RHO)
            self.output_layer_config['out_dims'].append(loss_config['num_components'])
            self.output_layer_config['out_activation_fn'].append(C.TANH)

        if loss_config['type'] in [C.NLL_BERNOULLI]:
            self.output_layer_config['out_keys'].append(loss_config['out_key']+C.SUF_BINARY)
            self.output_layer_config['out_dims'].append(self.target_dims[loss_config['target_idx']])
            self.output_layer_config['out_activation_fn'].append(C.SIGMOID)

        if loss_config['type'] == C.NLL_CENT:
            self.output_layer_config['out_keys'].append(loss_config['out_key'] + C.SUF_MU)
            self.output_layer_config['out_dims'].append(self.target_dims[loss_config['target_idx']])
            self.output_layer_config['out_activation_fn'].append(None)

        if loss_config['type'] == C.NLL_CENT_BINARY:
            self.output_layer_config['out_keys'].append(loss_config['out_key'] + C.SUF_MU)
            self.output_layer_config['out_dims'].append(self.target_dims[loss_config['target_idx']])
            self.output_layer_config['out_activation_fn'].append(None)

    def build_graph(self):
        """
        Called by TrainingEngine. Assembles modules of tensorflow computational graph by creating model, loss terms and
        summaries for tensorboard. Applies preprocessing on the inputs and postprocessing on model outputs if necessary.
        """
        raise NotImplementedError('subclasses must override build_graph method')

    def build_network(self):
        """
        Builds internal dynamics of the model. Sets
        """
        raise NotImplementedError('subclasses must override build_network method')

    def sample(self, **kwargs):
        """
        Draws samples from model.
        """
        raise NotImplementedError('subclasses must override sample method')

    def reconstruct(self, **kwargs):
        """
        Predicts the next step by using previous ground truth steps.
        Args:
            **kwargs:
        Returns:
            Predictions of next steps (batch_size, input_seq_len, feature_size)
        """
        raise NotImplementedError('subclasses must override reconstruct method')

    def build_loss_terms(self):
        """
        Builds loss terms.
        """
        # Function to get final loss value, i.e., average or sum.
        self.reduce_loss_fn = get_reduce_loss_func(self.config.get('reduce_loss'), tf.reduce_sum(self.seq_loss_mask, axis=[1, 2]))
        for loss_name, loss_entry in self.loss_config.items():
            loss_type = loss_entry['type']
            out_key = loss_entry['out_key']
            target_idx = loss_entry['target_idx']
            loss_key = "loss_" + loss_name
            op_loss_key = loss_name + "_loss"
            if loss_key not in self.ops_loss:
                with tf.name_scope(op_loss_key):
                    # Negative log likelihood loss.
                    if loss_type == C.NLL_NORMAL:
                        logli_term = tf_loss.logli_normal_isotropic(self.target_pieces[target_idx],
                                                                    self.ops_model_output[out_key + C.SUF_MU],
                                                                    self.ops_model_output[out_key + C.SUF_SIGMA],
                                                                    reduce_sum=False)
                    elif loss_type == C.NLL_BINORMAL:
                        logli_term = tf_loss.logli_normal_bivariate(self.target_pieces[target_idx],
                                                                    self.ops_model_output[out_key + C.SUF_MU],
                                                                    self.ops_model_output[out_key + C.SUF_SIGMA],
                                                                    self.ops_model_output[out_key + C.SUF_RHO],
                                                                    reduce_sum=False)
                    elif loss_type == C.NLL_GMM:
                        logli_term = tf_loss.logli_gmm_logsumexp(self.target_pieces[target_idx],
                                                                 self.ops_model_output[out_key + C.SUF_MU],
                                                                 self.ops_model_output[out_key + C.SUF_SIGMA],
                                                                 self.ops_model_output[out_key + C.SUF_COEFFICIENT])
                    elif loss_type == C.NLL_BERNOULLI:
                        logli_term = tf_loss.logli_bernoulli(self.target_pieces[target_idx],
                                                             self.ops_model_output[out_key + C.SUF_BINARY])
                    elif loss_type == C.MSE:
                        logli_term = -tf.reduce_sum(tf.square((self.target_pieces[target_idx] - self.ops_model_output[out_key + C.SUF_MU])), axis=2, keepdims=True)
                    elif loss_type == C.NLL_CENT:
                        labels = self.target_pieces[target_idx]
                        logits = self.ops_model_output[out_key + C.SUF_MU]
                        logli_term = tf.expand_dims(-tf.nn.softmax_cross_entropy_with_logits_v2(labels=labels, logits=logits), axis=-1)
                    elif loss_type == C.NLL_CENT_BINARY:
                        labels = self.target_pieces[target_idx]
                        logits = self.ops_model_output[out_key + C.SUF_MU]
                        logli_term = -tf.nn.sigmoid_cross_entropy_with_logits(labels=labels, logits=logits)
                        # Get model's predicted probabilities for binary outputs.
                        self.ops_evaluation["out_probability"] = tf.nn.sigmoid(logits)
                    else:
                        raise Exception(loss_type + " is not implemented.")

                    self.likelihood += logli_term
                    loss_term = -loss_entry['weight']*self.reduce_loss_fn(self.seq_loss_mask*logli_term)
                    self.ops_loss[loss_key] = loss_term

    def build_total_loss(self):
        """
        Accumulate losses to create training optimization. Model.loss is used by the optimization function.
        """
        self.loss = 0
        for loss_key, loss_op in self.ops_loss.items():
            # Optimization is done by only using "loss*" terms.
            if loss_key.startswith("loss"):
                self.loss += loss_op
        self.ops_loss['total_loss'] = self.loss

    def build_summary_plots(self):
        """
        Creates scalar summaries for loss plots. Iterates through `ops_loss` member and create a summary entry.

        If the model is in `validation` mode, then we follow a different strategy. In order to have a consistent
        validation report over iterations, we first collect model performance on every validation mini-batch
        and then report the average loss. Due to tensorflow's lack of loss averaging ops, we need to create
        placeholders per loss to pass the average loss.
        """
        if self.is_training:
            # For each loss term, create a tensorboard plot.
            for loss_name, loss_op in self.ops_loss.items():
                tf.summary.scalar(loss_name, loss_op, collections=[self.mode + '_summary_plot', self.mode + '_loss'])

        else:
            # Validation: first accumulate losses and then plot.
            # Create containers and placeholders for every loss term. After each validation step, keeps summing losses.
            # At the end of validation loop, calculates average performance on the whole validation dataset and creates
            # summary entries.
            for loss_name, _ in self.ops_loss.items():
                self.container_loss[loss_name] = 0
                self.container_loss_placeholders[loss_name] = tf.placeholder(tf.float32, shape=[])
                tf.summary.scalar(loss_name, self.container_loss_placeholders[loss_name], collections=[self.mode + '_summary_plot', self.mode + '_loss'])
                self.container_validation_feed_dict[self.container_loss_placeholders[loss_name]] = 0.0

    def finalise_graph(self):
        """
        Finalises graph building. It is useful if child classes must create some ops first.
        """
        self.loss_summary = tf.summary.merge_all(self.mode + '_summary_plot')
        if self.is_training:
            self.register_run_ops('summary', self.loss_summary)

        self.register_run_ops('loss', self.ops_loss)
        self.register_run_ops('batch_size', tf.shape(self.pl_seq_length)[0])

    def training_step(self, step, epoch, feed_dict=None):
        """
        Training loop function. Takes a batch of samples, evaluates graph ops and updates model parameters.

        Args:
            step: current step.
            epoch: current epoch.
            feed_dict (dict): feed dictionary.

        Returns (dict): evaluation results.
        """
        start_time = time.perf_counter()
        ops_run_loop_results = self.session.run(self.ops_run_loop, feed_dict=feed_dict)

        if math.isnan(ops_run_loop_results['loss']['total_loss']):
            raise Exception("NaN values.")

        if step % self.print_every_step == 0:
            time_elapsed = (time.perf_counter() - start_time)
            self.log_loss(ops_run_loop_results['loss'], step, epoch, time_elapsed, prefix=self.mode + ": ")

        return ops_run_loop_results

    def evaluation_step(self, step, epoch, num_iterations, feed_dict=None):
        """
        Evaluation loop function. Evaluates the whole validation/test dataset and logs performance.

        Args:
            step: current step.
            epoch: current epoch.
            num_iterations: number of steps.
            feed_dict (dict): feed dictionary.

        Returns: summary object.
        """
        self.reset_validation_loss()
        start_time = time.perf_counter()
        for i in range(num_iterations):
            ops_run_loop_results = self.session.run(self.ops_run_loop, feed_dict=feed_dict)
            self.update_validation_loss(ops_run_loop_results)

        summary, total_loss = self.get_validation_summary()

        time_elapsed = (time.perf_counter() - start_time)
        self.log_loss(total_loss, step, epoch, time_elapsed, prefix=self.mode + ": ")

        return summary, total_loss

    def evaluation_step_test_time(self, coord, threads, step, epoch, num_iterations, feed_dict=None):
        """
        Makes sure that all samples are used.
        """
        self.reset_validation_loss()
        start_time = time.perf_counter()
        for i in range(num_iterations-1):
            ops_run_loop_results = self.session.run(self.ops_run_loop, feed_dict=feed_dict)
            self.update_validation_loss(ops_run_loop_results)
        try:
            coord.request_stop()
            coord.join(threads, stop_grace_period_secs=0.5)
        except:
            pass
        ops_run_loop_results = self.session.run(self.ops_run_loop, feed_dict=feed_dict)
        self.update_validation_loss(ops_run_loop_results)

        summary, total_loss = self.get_validation_summary()
        time_elapsed = (time.perf_counter() - start_time)
        self.log_loss(total_loss, step, epoch, time_elapsed, prefix=self.mode + ": ")
        return summary, total_loss

    def log_loss(self, eval_loss, step=0, epoch=0, time_elapsed=None, prefix=""):
        """
        Prints status messages during training. It is called in the main training loop.
        Args:
            eval_loss (dict): evaluated results of `ops_loss` dictionary.
            step (int): current step.
            epoch (int): current epoch.
            time_elapsed (float): elapsed time.
            prefix (str): some informative text. For example, "training" or "validation".
        """
        loss_format = prefix + "{}/{} \t Total: {:.4f} \t"
        loss_entries = [step, epoch, eval_loss['total_loss']]

        for loss_key in sorted(eval_loss.keys()):
            if loss_key != 'total_loss':
                loss_format += "{}: {:.4f} \t"
                loss_entries.append(loss_key)
                loss_entries.append(eval_loss[loss_key])

        if time_elapsed is not None:
            print(loss_format.format(*loss_entries) + "time/batch = {:.3f}".format(time_elapsed))
        else:
            print(loss_format.format(*loss_entries))

    def register_run_ops(self, op_key, op):
        """
        Adds a new graph op into `self.ops_run_loop`.

        Args:
            op_key (str): dictionary key.
            op: tensorflow op

        Returns:
        """
        if op_key in self.ops_run_loop and isinstance(self.ops_run_loop[op_key], list):
            self.ops_run_loop[op_key].append(op)
        else:
            self.ops_run_loop[op_key] = op

        for key, op in self.ops_model_output.items():
            self.ops_run_loop[key] = op
        self.ops_run_loop["inputs"] = self.pl_inputs
        self.ops_run_loop["targets"] = self.pl_targets

    def flat_tensor(self, tensor, dim=-1):
        """
        Reshapes a tensor such that it has 2 dimensions. The dimension specified by `dim` is kept.
        """
        keep_dim_size = tensor.shape.as_list()[dim]
        return tf.reshape(tensor, [-1, keep_dim_size])

    def temporal_tensor(self, flat_tensor):
        """
        Reshapes a flat tensor (2-dimensional) to a tensor with shape (batch_size, seq_len, feature_size). Assuming
        that the flat tensor has shape of (batch_size*seq_len, feature_size).
        """
        feature_size = flat_tensor.shape.as_list()[-1]
        return tf.reshape(flat_tensor, [self.batch_size, -1, feature_size])

    def log_num_parameters(self):
        """
        Prints total number of parameters.
        """
        num_param = 0
        for v in tf.global_variables():
            num_param += np.prod(v.shape.as_list())

        self.num_parameters = num_param
        print("# of parameters: " + str(num_param))
        self.config.set('total_parameters', int(self.num_parameters), override=True)

    ########################################
    # Summary methods for validation mode.
    ########################################
    def update_validation_loss(self, loss_evaluated):
        """
        Updates validation losses. Note that this method is called after every validation step.

        Args:
            loss_evaluated: valuated results of `ops_loss` dictionary.
        """
        batch_size = loss_evaluated["batch_size"]
        self.validation_summary_num_runs += batch_size
        for loss_name, loss_value in loss_evaluated["loss"].items():
            self.container_loss[loss_name] += (loss_value*batch_size)

    def reset_validation_loss(self):
        """
        Resets validation loss containers.
        """
        self.validation_summary_num_runs = 0
        for loss_name, loss_value in self.container_loss.items():
            self.container_loss[loss_name] = 0

    def get_validation_summary(self):
        """
        Creates a feed dictionary of validation losses for validation summary. Note that this method is called after
        validation loops is over.

        Returns (dict, dict):
            feed_dict for validation summary.
            average `ops_loss` results for `log_loss` method.
        """
        for loss_name, loss_pl in self.container_loss_placeholders.items():
            self.container_loss[loss_name] /= self.validation_summary_num_runs
            self.container_validation_feed_dict[loss_pl] = self.container_loss[loss_name]

        self.validation_summary_num_runs = 0
        valid_summary = self.session.run(self.loss_summary, self.container_validation_feed_dict)
        return valid_summary, self.container_loss


class TCN(BaseTemporalModel):
    """
    Causal convolutional network from `Wavenet: A Generative Model for Raw Audio` (https://arxiv.org/abs/1609.03499)
    paper.
    """
    def __init__(self, config, session, reuse, mode, placeholders, input_dims, target_dims, **kwargs):
        super(TCN, self).__init__(config, session, reuse, mode, placeholders, input_dims, target_dims, **kwargs)

        self.input_layer_config = config.get('input_layer', None)
        self.cnn_layer_config = config.get('cnn_layer')
        self.use_gate = self.cnn_layer_config.get('use_gating', False)
        self.use_residual = self.cnn_layer_config.get('use_residual', False)
        self.use_skip = self.cnn_layer_config.get('use_skip', False)
        # Concatenates representations of these layers for the outputs.
        self.tcn_output_layer_idx = self.cnn_layer_config.get('tcn_output_layer_idx', [-1])
        # If True, at every layer the input sequence is padded with zeros at the beginning such that the output length
        # becomes equal to the input length.
        self.zero_padding = self.cnn_layer_config.get('zero_padding', False)
        self.activation_fn = get_activation_fn(self.cnn_layer_config['activation_fn'])

        # Output of temporal convolutional layers.
        self.temporal_block_outputs = None

        # Model's receptive field length.
        self.receptive_field_width = None
        # Model's output length. If self.zero_padding is True, then it is the same as self.pl_seq_length.
        self.output_width = None

    @staticmethod
    def receptive_field_size_zero_padding(filter_size, dilation_size_list):
        # 2* is due to the second causal convolution layer in a temporal block.
        # TODO Not sure if this is correct. For now we don't need it.
        return 2*(filter_size - 1)*sum(dilation_size_list) + 1

    @staticmethod
    def receptive_field_size(filter_size, dilation_size_list):
        return (filter_size - 1)*sum(dilation_size_list) + 1

    @staticmethod
    def causal_conv_layer(input_layer, num_filters, kernel_size, dilation, zero_padding, activation_fn):
        padded_input_layer = input_layer
        # Applies padding at the start of the sequence with (kernel_size-1)*dilation zeros.
        padding_steps = (kernel_size - 1)*dilation
        if zero_padding and padding_steps > 0:
            padded_input_layer = tf.pad(input_layer, tf.constant([(0, 0,), (1, 0), (0, 0)])*padding_steps, mode='CONSTANT')
            input_shape = input_layer.shape.as_list()
            if input_shape[1] is not None:
                input_shape[1] += padding_steps
            padded_input_layer.set_shape(input_shape)

        conv_layer = tf.layers.conv1d(inputs=padded_input_layer,
                                      filters=num_filters,
                                      kernel_size=kernel_size,
                                      strides=1,
                                      padding='valid',
                                      dilation_rate=dilation,
                                      activation=activation_fn)
        return conv_layer

    @staticmethod
    def causal_gated_layer(input_layer, kernel_size, num_filters, dilation, zero_padding):
        with tf.name_scope('filter_conv'):
            filter_op = TCN.causal_conv_layer(input_layer=input_layer,
                                              num_filters=num_filters,
                                              kernel_size=kernel_size,
                                              dilation=dilation,
                                              zero_padding=zero_padding,
                                              activation_fn=tf.nn.tanh)
        with tf.name_scope('gate_conv'):
            gate_op = TCN.causal_conv_layer(input_layer=input_layer,
                                            num_filters=num_filters,
                                            kernel_size=kernel_size,
                                            dilation=dilation,
                                            zero_padding=zero_padding,
                                            activation_fn=tf.nn.sigmoid)
        with tf.name_scope('gating'):
            gated_dilation = gate_op*filter_op

        return gated_dilation

    @staticmethod
    def temporal_block(input_layer, num_filters, kernel_size, dilation, activation_fn, use_gate=True, use_residual=True, zero_padding=False):
        if use_gate:
            with tf.name_scope('gated_causal_layer'):
                temp_out = TCN.causal_gated_layer(input_layer=input_layer,
                                                  kernel_size=kernel_size,
                                                  num_filters=num_filters,
                                                  dilation=dilation,
                                                  zero_padding=zero_padding)
        else:
            with tf.name_scope('causal_layer'):
                temp_out = TCN.causal_conv_layer(input_layer=input_layer,
                                                 kernel_size=kernel_size,
                                                 num_filters=num_filters,
                                                 dilation=dilation,
                                                 zero_padding=zero_padding,
                                                 activation_fn=activation_fn)
        with tf.name_scope('block_output'):
            temp_out = tf.layers.conv1d(inputs=temp_out,
                                        filters=num_filters,
                                        kernel_size=1,
                                        padding='valid',
                                        dilation_rate=1,
                                        activation=None)

        skip_out = temp_out
        if use_residual:
            with tf.name_scope('residual_layer'):
                res_layer = input_layer
                if input_layer.shape[2] != num_filters:
                    res_layer = tf.layers.conv1d(inputs=input_layer,
                                                 filters=num_filters,
                                                 kernel_size=1,
                                                 padding='valid',
                                                 dilation_rate=1,
                                                 activation=None)
                if zero_padding is False:
                    # Cut off input sequence so that it has the same width with outputs.
                    input_width_res = tf.shape(res_layer)[1] - tf.shape(temp_out)[1]
                    res_layer = tf.slice(res_layer, [0, input_width_res, 0], [-1, -1, -1])

                temp_out = temp_out + res_layer

        return temp_out, skip_out

    def build_graph(self):
        """
        Builds model and creates plots for tensorboard. Decomposes model building into sub-modules and makes inheritance
        is easier.
        """
        self.build_network()
        self.build_loss_terms()
        self.build_total_loss()
        self.build_summary_plots()
        self.finalise_graph()
        if self.reuse is False:
            self.log_num_parameters()

    def build_network(self):
        self.build_input_layer()
        current_layer = self.inputs_hidden

        self.receptive_field_width = TCN.receptive_field_size(self.cnn_layer_config['filter_size'], self.cnn_layer_config['dilation_size'])
        if self.zero_padding is True:
            self.output_width = tf.shape(current_layer)[1]
        else:
            self.output_width = tf.shape(current_layer)[1] - self.receptive_field_width + 1

        # Initial causal convolution layer mapping inputs to a space with number of dilation filters dimensions.
        with tf.variable_scope('causal_conv_layer_0', reuse=self.reuse):
            current_layer = TCN.causal_conv_layer(input_layer=current_layer,
                                                  num_filters=self.cnn_layer_config['num_filters'],
                                                  kernel_size=self.cnn_layer_config['filter_size'],
                                                  dilation=1,
                                                  zero_padding=self.zero_padding,
                                                  activation_fn=None)
        # Stack causal convolutional layers.
        out_layers, skip_layers = self.build_temporal_block(current_layer, self.cnn_layer_config['num_layers'], self.reuse, self.cnn_layer_config['filter_size'])

        if self.use_skip:
            # Sum skip connections from the outputs of each layer.
            self.temporal_block_outputs = self.activation_fn(sum(skip_layers))
        else:
            tcn_output_layers = []
            for idx in self.tcn_output_layer_idx:
                tcn_output_layers.append(out_layers[idx])

            self.temporal_block_outputs = self.activation_fn(tf.concat(tcn_output_layers, axis=-1))
        self.build_output_layer()

    def build_input_layer(self):
        """
        Builds a number fully connected layers projecting the inputs into an intermediate representation  space.
        """
        self.inputs_hidden = self.pl_inputs
        if self.input_layer_config is not None:
            if self.input_layer_config.get("dropout_rate", 0) > 0:
                with tf.variable_scope('input_layer', reuse=self.reuse):
                    self.inputs_hidden = tf.layers.dropout(self.pl_inputs,
                                                           rate=self.input_layer_config.get("dropout_rate"),
                                                           noise_shape=None,
                                                           seed=self.config.seed,
                                                           training=self.is_training)

    def build_temporal_block(self, input_layer, num_layers, reuse, kernel_size=2):
        """
        Stacks a number of causal convolutional layers.
        """
        current_layer = input_layer
        temporal_blocks = []
        temporal_blocks_no_res = []
        for idx in range(num_layers):
            with tf.variable_scope('temporal_block_' + str(idx + 1), reuse=reuse):
                temp_block, temp_wo_res = TCN.temporal_block(input_layer=current_layer,
                                                             num_filters=self.cnn_layer_config['num_filters'],
                                                             kernel_size=kernel_size,
                                                             dilation=self.cnn_layer_config['dilation_size'][idx],
                                                             activation_fn=self.activation_fn, use_gate=self.use_gate,
                                                             use_residual=self.use_residual,
                                                             zero_padding=self.zero_padding)
                temporal_blocks_no_res.append(temp_wo_res)
                temporal_blocks.append(temp_block)
                current_layer = temp_block

        return temporal_blocks, temporal_blocks_no_res

    def build_output_layer(self):
        """
        Builds a number fully connected layers projecting CNN representations onto output space. Then, outputs are
        predicted by linear layers.

        Returns:
        """
        with tf.variable_scope('output_layer_hidden', reuse=self.reuse):
            current_layer = self.temporal_block_outputs
            for idx in range(self.output_layer_config.get('num_layers', 1)):
                with tf.variable_scope('conv1d_' + str(idx + 1), reuse=self.reuse):
                    current_layer = tf.layers.conv1d(inputs=current_layer, kernel_size=1, padding='valid',
                                                     filters=self.cnn_layer_config['num_filters'], dilation_rate=1,
                                                     activation=self.activation_fn)
            outputs_hidden = current_layer
        for idx in range(len(self.output_layer_config['out_keys'])):
            key = self.output_layer_config['out_keys'][idx]
            with tf.variable_scope('output_layer_' + key, reuse=self.reuse):
                output = tf.layers.conv1d(inputs=outputs_hidden,
                                          filters=self.output_layer_config['out_dims'][idx],
                                          kernel_size=1,
                                          padding='valid',
                                          activation=get_activation_fn(self.output_layer_config['out_activation_fn'][idx]))
                self.ops_model_output[key] = output

        # Trim initial steps corresponding to the receptive field.
        self.seq_loss_mask = tf.slice(self.seq_loss_mask, [0, tf.shape(self.seq_loss_mask)[1] - self.output_width, 0], [-1, -1, -1])
        for idx, target in enumerate(self.target_pieces):
            self.target_pieces[idx] = tf.slice(target, [0, tf.shape(target)[1] - self.output_width, 0], [-1, -1, -1])

        num_entries = tf.cast(tf.reduce_sum(self.seq_loss_mask), tf.float32)*tf.cast(tf.shape(self.ops_model_output[C.OUT_MU])[-1], tf.float32)
        if C.OUT_MU in self.ops_model_output:
            self.ops_scalar_summary["mean_out_mu"] = tf.reduce_sum(self.ops_model_output[C.OUT_MU]*self.seq_loss_mask)/num_entries
        if C.OUT_SIGMA in self.ops_model_output:
            self.ops_scalar_summary["mean_out_sigma"] = tf.reduce_sum(self.ops_model_output[C.OUT_SIGMA]*self.seq_loss_mask)/num_entries

        self.output_sample = self.sample_fn_tf(self.ops_model_output)
        self.input_sample = self.pl_inputs
        self.ops_evaluation['sample'] = self.output_sample

    def reconstruct(self, **kwargs):
        """
        Predicts the next step by using previous ground truth steps. If the target sequence is passed, then loss is also
        reported.
        Args:
            **kwargs:

        Returns:
            Predictions of next steps (batch_size, input_seq_len, feature_size). Due to causality constraint, number of
            prediction steps is input_seq_len-receptive_field_width. We simply take the first <receptive_field_width>
            many steps from the input sequence to pad reconstructed sequence.
        """
        input_sequence = kwargs.get('input_sequence', None)
        target_sequence = kwargs.get('target_sequence', None)

        assert input_sequence is not None, "Need an input sample."
        batch_dimension = input_sequence.ndim == 3
        if batch_dimension is False:
            input_sequence = np.expand_dims(input_sequence, axis=0)
        input_seq_len = input_sequence.shape[1]
        if self.zero_padding is False:
            assert input_seq_len >= self.receptive_field_width, "Input sequence should have at least " + str(self.receptive_field_width) + " steps."

        feed_dict = {self.pl_inputs: input_sequence}

        if target_sequence is not None:
            if batch_dimension is False:
                target_sequence = np.expand_dims(target_sequence, axis=0)

            if "loss" not in self.ops_evaluation:
                self.ops_evaluation['loss'] = self.ops_loss

            feed_dict[self.pl_targets] = target_sequence
            feed_dict[self.pl_seq_length] = np.array([target_sequence.shape[1]]*target_sequence.shape[0])

        model_outputs = self.session.run(self.ops_evaluation, feed_dict)
        if "loss" in model_outputs:
            self.log_loss(model_outputs['loss'])

        if batch_dimension is False:
            model_outputs["sample"] = model_outputs["sample"][0]

        return model_outputs

    def sample(self, **kwargs):
        """
        Sampling function.
        Args:
            **kwargs:
        """
        seed_sequence = kwargs.get('seed_sequence', None)
        sample_length = kwargs.get('sample_length', 100)

        assert seed_sequence is not None, "Need a seed sample."
        batch_dimension = seed_sequence.ndim == 3
        if batch_dimension is False:
            seed_sequence = np.expand_dims(seed_sequence, axis=0)
        seed_len = seed_sequence.shape[1]
        if self.zero_padding is False:
            assert seed_len >= self.receptive_field_width, "Seed sequence should have at least " + str(self.receptive_field_width) + " steps."

        model_input = seed_sequence[:, -self.receptive_field_width:]
        model_outputs = self.sample_function(model_input, sample_length)

        if batch_dimension is False:
            model_outputs["sample"] = model_outputs["sample"][0]

        return model_outputs

    def sample_function(self, model_input, sample_length):
        """
        Auxiliary method to draw sequence of samples in auto-regressive fashion.
        Args:
            model_input (batch_size, seq_len, feature_size): seed sequence which must have at least
                self.receptive_field_width many steps.
            sample_length (int): number of sample steps.

        Returns:
            Synthetic samples as numpy array (batch_size, sample_length, feature_size)
        """
        sequence = model_input.copy()
        for step in range(sample_length):
            model_input = sequence[:, -self.receptive_field_width:]
            model_outputs = self.session.run(self.ops_evaluation, feed_dict={self.pl_inputs: model_input})

            next_step = model_outputs['sample'][:, -1:]
            sequence = np.concatenate([sequence, next_step], axis=1)
        return {"sample": sequence[:, -sample_length:]}


class StochasticTCN(TCN):
    """
    Temporal convolutional model with stochastic latent space.
    """
    def __init__(self, config, session, reuse, mode, placeholders, input_dims, target_dims, **kwargs):
        super(StochasticTCN, self).__init__(config, session, reuse, mode, placeholders, input_dims, target_dims, **kwargs)

        # Inputs to the decoder or output layer.
        self.decoder_use_enc_skip = self.config.get('decoder_use_enc_skip', False)
        self.decoder_use_enc_last = self.config.get('decoder_use_enc_last', False)
        self.decoder_use_raw_inputs = self.config.get('decoder_use_raw_inputs', False)

        self.num_encoder_blocks = self.cnn_layer_config.get('num_encoder_layers')
        self.num_decoder_blocks = self.cnn_layer_config.get('num_decoder_layers')

        # Add latent layer related fields.
        self.latent_layer_config = self.config.get("latent_layer")
        self.latent_layer = LatentLayer.get(self.latent_layer_config["type"], self.latent_layer_config, mode, reuse, global_step=self.global_step)

        # List of temporal convolution layers that are used in encoder.
        self.encoder_blocks = []
        self.encoder_blocks_no_res = []
        # List of temporal convolution layers that are used in decoder.
        self.decoder_blocks = []
        self.decoder_blocks_no_res = []

        self.bw_encoder_blocks = []
        self.bw_encoder_blocks_no_res = []

    def build_network(self):
        # We always pad the input sequences such that the output sequence has the same length with input sequence.
        self.receptive_field_width = TCN.receptive_field_size(self.cnn_layer_config['filter_size'], self.cnn_layer_config['dilation_size'])

        # Shift the input sequence by one step so that the task is prediction of the next step.
        with tf.name_scope("input_padding"):
            shifted_inputs = tf.pad(self.pl_inputs, tf.constant([(0, 0,), (1, 0), (0, 0)]), mode='CONSTANT')

        self.inputs_hidden = shifted_inputs
        if self.input_layer_config is not None and self.input_layer_config.get("dropout_rate", 0) > 0:
            with tf.variable_scope('input_dropout', reuse=self.reuse):
                self.inputs_hidden = tf.layers.dropout(shifted_inputs, rate=self.input_layer_config.get("dropout_rate"), seed=self.config.seed, training=self.is_training)

        with tf.variable_scope("encoder", reuse=self.reuse):
            self.encoder_blocks, self.encoder_blocks_no_res = self.build_temporal_block(self.inputs_hidden, self.num_encoder_blocks, self.reuse, self.cnn_layer_config['filter_size'])

        with tf.variable_scope("latent", reuse=self.reuse):
            p_input = [enc_layer[:, 0:-1] for enc_layer in self.encoder_blocks]
            if self.latent_layer_config.get('dynamic_prior', False):
                q_input = [enc_layer[:, 1:] for enc_layer in self.encoder_blocks]
            else:
                q_input = p_input
            latent_sample = self.latent_layer.build_latent_layer(q_input=q_input,
                                                                 p_input=p_input,
                                                                 output_ops_dict=self.ops_model_output,
                                                                 eval_ops_dict=self.ops_evaluation,
                                                                 summary_ops_dict=self.ops_scalar_summary)

        # Build causal decoder blocks if we have any. Otherwise, we just use a number of 1x1 convolutions in
        # build_output_layer. Note that there are several input options.
        decoder_inputs = [latent_sample]
        if self.decoder_use_enc_skip:
            skip_connections = [enc_layer[:, 0:-1] for enc_layer in self.encoder_blocks_no_res]
            decoder_inputs.append(self.activation_fn(sum(skip_connections)))
        if self.decoder_use_enc_last:
            decoder_inputs.append(self.encoder_blocks[-1][:, 0:-1])  # Top-most convolutional layer.
        if self.decoder_use_raw_inputs:
            decoder_inputs.append(shifted_inputs[:, 0:-1])

        if self.num_decoder_blocks > 0:
            with tf.variable_scope("decoder", reuse=self.reuse):
                decoder_input_layer = tf.concat(decoder_inputs, axis=-1)
                decoder_filter_size = self.cnn_layer_config.get("decoder_filter_size", self.cnn_layer_config['filter_size'])
                self.decoder_blocks, self.decoder_blocks_no_res = self.build_temporal_block(decoder_input_layer,
                                                                                            self.num_decoder_blocks,
                                                                                            self.reuse,
                                                                                            kernel_size=decoder_filter_size)
                self.temporal_block_outputs = self.decoder_blocks[-1]
        else:
            self.temporal_block_outputs = tf.concat(decoder_inputs, axis=-1)

        self.output_width = tf.shape(latent_sample)[1]
        self.build_output_layer()

    def build_temporal_block(self, input_layer, num_layers, reuse, kernel_size=2):
        current_layer = input_layer
        temporal_blocks = []
        temporal_blocks_no_res = []
        for idx in range(num_layers):
            with tf.variable_scope('temporal_block_' + str(idx + 1), reuse=reuse):
                temp_block, temp_wo_res = TCN.temporal_block(input_layer=current_layer,
                                                             num_filters=self.cnn_layer_config['num_filters'],
                                                             kernel_size=kernel_size,
                                                             dilation=self.cnn_layer_config['dilation_size'][idx],
                                                             activation_fn=self.activation_fn, use_gate=self.use_gate,
                                                             use_residual=self.use_residual,
                                                             zero_padding=self.zero_padding)
                temporal_blocks_no_res.append(temp_wo_res)
                temporal_blocks.append(temp_block)
                current_layer = temp_block

        return temporal_blocks, temporal_blocks_no_res

    def build_output_layer(self):
        """
        Builds layers to make predictions.
        """
        out_layer_type = self.output_layer_config.get('type', None)
        if out_layer_type is None:
            out_layer_type = C.LAYER_TCN

        with tf.variable_scope('output_layer', reuse=self.reuse):
            current_layer = self.temporal_block_outputs
            num_filters = self.cnn_layer_config['num_filters'] if self.output_layer_config.get('size', 0) < 1 else self.output_layer_config.get('size')

            if out_layer_type == C.LAYER_CONV1:
                for idx in range(self.output_layer_config.get('num_layers', 1)):
                    with tf.variable_scope('out_conv1d_' + str(idx + 1), reuse=self.reuse):
                        current_layer = tf.layers.conv1d(inputs=current_layer, kernel_size=1, padding='valid',
                                                         filters=num_filters, dilation_rate=1,
                                                         activation=self.activation_fn)
            if out_layer_type == C.LAYER_TCN:
                kernel_size = self.cnn_layer_config['filter_size'] if self.output_layer_config.get('filter_size', 0) < 1 else self.output_layer_config.get('filter_size', 0)
                for idx in range(self.output_layer_config.get('num_layers', 1)):
                    with tf.variable_scope('out_convCCN_' + str(idx + 1), reuse=self.reuse):
                        current_layer, _ = TCN.temporal_block(input_layer=current_layer, num_filters=num_filters,
                                                              kernel_size=kernel_size, dilation=1,
                                                              activation_fn=self.activation_fn,
                                                              use_gate=self.use_gate,
                                                              use_residual=self.use_residual, zero_padding=True)
            for idx in range(len(self.output_layer_config['out_keys'])):
                key = self.output_layer_config['out_keys'][idx]
                with tf.variable_scope('out_' + key, reuse=self.reuse):
                    out_activation = get_activation_fn(self.output_layer_config['out_activation_fn'][idx])
                    output = tf.layers.conv1d(inputs=current_layer,
                                              filters=self.output_layer_config['out_dims'][idx],
                                              kernel_size=1,
                                              padding='valid',
                                              activation=out_activation)
                    self.ops_model_output[key] = output

        self.seq_loss_mask = tf.slice(self.seq_loss_mask, [0, tf.shape(self.seq_loss_mask)[1] - self.output_width, 0], [-1, -1, -1])
        # for idx, target in enumerate(self.target_pieces):
        #    self.target_pieces[idx] = tf.slice(target, [0, tf.shape(target)[1] - self.output_width, 0], [-1, -1, -1])

        num_entries = tf.cast(tf.reduce_sum(self.seq_loss_mask), tf.float32)*tf.cast(tf.shape(self.ops_model_output[C.OUT_MU])[-1], tf.float32)
        if C.OUT_MU in self.ops_model_output:
            self.ops_scalar_summary["mean_out_mu"] = tf.reduce_sum(self.ops_model_output[C.OUT_MU]*self.seq_loss_mask)/num_entries
        if C.OUT_SIGMA in self.ops_model_output:
            self.ops_scalar_summary["mean_out_sigma"] = tf.reduce_sum(self.ops_model_output[C.OUT_SIGMA]*self.seq_loss_mask)/num_entries

        self.output_sample = self.sample_fn_tf(self.ops_model_output)
        self.input_sample = self.pl_inputs
        self.ops_evaluation['sample'] = self.output_sample

    def build_loss_terms(self):
        """
        Builds loss terms.
        """
        super(StochasticTCN, self).build_loss_terms()

        # Get latent layer loss terms, apply mask and reduce function, and insert into our loss container.
        if self.is_eval:
            self.latent_layer.build_loss(self.seq_loss_mask, self.reduce_loss_fn, self.ops_loss, reward=self.likelihood, eval_dict=self.ops_for_eval_mode)
            self.ops_evaluation["eval_dict"] = self.ops_for_eval_mode
        else:
            self.latent_layer.build_loss(self.seq_loss_mask, self.reduce_loss_fn, self.ops_loss, reward=self.likelihood)

    def build_summary_plots(self):
        super(StochasticTCN, self).build_summary_plots()

        # Create summaries to visualize distribution of latent variables.
        if self.config.get('tensorboard_verbose', 0) > 1:
            for idx, encoder_block in enumerate(self.encoder_blocks):
                plot_key = "encoder_block_" + str(idx + 1)
                tf.summary.histogram(plot_key, encoder_block, collections=[self.mode + '_summary_plot', self.mode + '_temporal_block_activations'])

            for idx, decoder_block in enumerate(self.decoder_blocks):
                plot_key = "decoder_block_" + str(idx + 1)
                tf.summary.histogram(plot_key, decoder_block, collections=[self.mode + '_summary_plot', self.mode + '_temporal_block_activations'])

    def sample_function(self, model_input, sample_length):
        """
        Update: From now on we assume that the causal relationship between the inputs and targets are handled by dataset.
        Hence, we don't need to insert a dummy step.

        Auxiliary method to draw sequence of samples in auto-regressive fashion. We use prior distribution to sample
        next step.
        Args:
            model_input (batch_size, seq_len, feature_size): seed sequence which must have at least
                self.receptive_field_width many steps.
            sample_length (int): number of sample steps.

        Returns:
            Synthetic samples as numpy array (batch_size, sample_length, feature_size)
        """

        assert self.is_sampling, "The model must be in sampling mode."
        # For each evaluation op, create a dummy output.
        output_dict = dict()
        for key, op in self.ops_evaluation.items():
            output_dict[key] = np.zeros((model_input.shape[0], 0, op.shape[2]))
        output_dict["sample"] = model_input.copy()

        dummy_x = np.zeros([model_input.shape[0], 1, model_input.shape[2]])
        for step in range(sample_length):
            model_inputs = np.concatenate([output_dict["sample"], dummy_x], axis=1)
            end_idx = min(self.receptive_field_width, model_inputs.shape[1])
            model_inputs = model_inputs[:, -end_idx:]
            feed_dict = dict()
            feed_dict[self.pl_inputs] = model_inputs
            feed_dict[self.pl_seq_length] = np.array([model_inputs.shape[1]]*model_inputs.shape[0])
            model_outputs = self.session.run(self.ops_evaluation, feed_dict=feed_dict)

            for key, val in model_outputs.items():
                output_dict[key] = np.concatenate([output_dict[key], val[:, -1:]], axis=1)

        output_dict["sample"] = output_dict["sample"][:, -sample_length:]
        return output_dict


class BaseRNN(BaseTemporalModel):
    """
    Implements abstract build_graph and build_network methods to build an RNN model.
    """
    def __init__(self, config, session, reuse, mode, placeholders, input_dims, target_dims, **kwargs):
        super(BaseRNN, self).__init__(config, session, reuse, mode, placeholders, input_dims, target_dims, **kwargs)

        self.input_layer_config = config.get('input_layer')
        self.cell_config = config.get('rnn_layer')

        self.cell = None  # RNN cell.
        self.initial_states = None  # Initial cell state.
        self.rnn_outputs = None  # Output of RNN layer.
        self.rnn_output_state = None  # Final state of RNN layer.
        self.output_layer_inputs = None  # Input to output layer.

    def build_graph(self):
        """
        Builds model and creates plots for tensorboard. Decomposes model building into sub-modules and makes inheritance
        is easier.
        """
        self.build_network()
        self.build_loss_terms()
        self.build_total_loss()
        self.build_summary_plots()
        self.finalise_graph()
        if self.reuse is False:
            self.log_num_parameters()

    def build_network(self):
        self.build_cell()
        self.build_input_layer()
        self.build_rnn_layer()
        self.build_output_layer()

    def build_cell(self):
        """
        Builds a Tensorflow RNN cell object by using the given configuration `self.cell_config`.
        """
        self.cell = get_rnn_cell(scope='rnn_cell', reuse=self.reuse, **self.cell_config)
        self.initial_states = self.cell.zero_state(batch_size=self.batch_size, dtype=tf.float32)

    def build_input_layer(self):
        """
        Builds a number fully connected layers projecting the inputs into an intermediate representation  space.
        """
        if self.input_layer_config is not None:
            with tf.variable_scope('input_layer', reuse=self.reuse):
                if self.input_layer_config.get("dropout_rate", 0) > 0:
                    self.inputs_hidden = tf.layers.dropout(self.pl_inputs,
                                                           rate=self.input_layer_config.get("dropout_rate"),
                                                           noise_shape=None,
                                                           seed=17,
                                                           training=self.is_training)
                else:
                    self.inputs_hidden = self.pl_inputs

                if self.input_layer_config.get("num_layers", 0) > 0:
                    flat_inputs_hidden = self.flat_tensor(self.inputs_hidden)
                    flat_inputs_hidden = fully_connected_layer(flat_inputs_hidden, **self.input_layer_config)
                    self.inputs_hidden = self.temporal_tensor(flat_inputs_hidden)
        else:
            self.inputs_hidden = self.pl_inputs

    def build_rnn_layer(self):
        """
        Builds RNN layer by using dynamic_rnn wrapper of Tensorflow.
        """
        with tf.variable_scope("rnn_layer", reuse=self.reuse):
            self.rnn_outputs, self.rnn_output_state = tf.nn.dynamic_rnn(self.cell,
                                                                        self.inputs_hidden,
                                                                        sequence_length=self.pl_seq_length,
                                                                        initial_state=self.initial_states,
                                                                        dtype=tf.float32)
            self.output_layer_inputs = self.rnn_outputs
            self.ops_evaluation['state'] = self.rnn_output_state

    def build_output_layer(self):
        """
        Builds a number fully connected layers projecting RNN predictions into an embedding space. Then, for each model
        output is predicted by a linear layer.
        """
        flat_outputs_hidden = self.flat_tensor(self.output_layer_inputs)
        with tf.variable_scope('output_layer_hidden', reuse=self.reuse):
            flat_outputs_hidden = fully_connected_layer(flat_outputs_hidden, is_training=self.is_training, **self.output_layer_config)

        for idx in range(len(self.output_layer_config['out_keys'])):
            key = self.output_layer_config['out_keys'][idx]

            with tf.variable_scope('output_layer_' + key, reuse=self.reuse):
                flat_out = linear(input_layer=flat_outputs_hidden,
                                  output_size=self.output_layer_config['out_dims'][idx],
                                  activation_fn=self.output_layer_config['out_activation_fn'][idx],
                                  is_training=self.is_training)

                self.ops_model_output[key] = self.temporal_tensor(flat_out)

        self.output_sample = self.sample_fn_tf(self.ops_model_output)
        self.input_sample = self.pl_inputs
        self.ops_evaluation['sample'] = self.output_sample


class RNNAutoRegressive(BaseRNN):
    """
    Auto-regressive RNN model. Predicts next step (t+1) given the current step (t). Note that here we assume targets are
    equivalent to inputs shifted by one step in time.
    """
    def __init__(self, config, session, reuse, mode, placeholders, input_dims, target_dims, **kwargs):
        super(RNNAutoRegressive, self).__init__(config, session, reuse, mode, placeholders, input_dims, target_dims, )

    def build_output_layer(self):
        # Prediction layer.
        BaseRNN.build_output_layer(self)

        num_entries = tf.cast(tf.reduce_sum(self.seq_loss_mask), tf.float32)*tf.cast(tf.shape(self.ops_model_output[C.OUT_MU])[-1], tf.float32)
        if C.OUT_MU in self.ops_model_output:
            self.ops_scalar_summary["mean_out_mu"] = tf.reduce_sum(self.ops_model_output[C.OUT_MU]*self.seq_loss_mask)/num_entries
        if C.OUT_SIGMA in self.ops_model_output:
            self.ops_scalar_summary["mean_out_sigma"] = tf.reduce_sum(self.ops_model_output[C.OUT_SIGMA]*self.seq_loss_mask)/num_entries

        self.output_sample = self.sample_fn_tf(self.ops_model_output)
        self.input_sample = self.pl_inputs
        self.ops_evaluation['sample'] = self.output_sample

    def reconstruct(self, **kwargs):
        """
        Predicts the next step by using previous ground truth steps. If the target sequence is passed, then loss is also
        reported.
        Args:
            **kwargs:
        Returns:
            Predictions of next steps (batch_size, input_seq_len, feature_size)
        """
        input_sequence = kwargs.get('input_sequence', None)
        target_sequence = kwargs.get('target_sequence', None)

        assert input_sequence is not None, "Need an input sample."
        batch_dimension = input_sequence.ndim == 3
        if batch_dimension is False:
            input_sequence = np.expand_dims(input_sequence, axis=0)

        feed_dict = dict()
        feed_dict[self.pl_inputs] = input_sequence
        feed_dict[self.pl_seq_length] = np.array([input_sequence.shape[1]]*input_sequence.shape[0])

        if target_sequence is not None:
            if batch_dimension is False:
                target_sequence = np.expand_dims(target_sequence, axis=0)

            if "loss" not in self.ops_evaluation:
                self.ops_evaluation['loss'] = self.ops_loss
            feed_dict[self.pl_targets] = target_sequence

        model_outputs = self.session.run(self.ops_evaluation, feed_dict)
        if "loss" in model_outputs:
            self.log_loss(model_outputs['loss'])

        if batch_dimension is False:
            model_outputs["sample"] = model_outputs["sample"][0]

        return model_outputs

    def sample(self, **kwargs):
        """
        Sampling function.
        Args:
            **kwargs:
        """
        seed_sequence = kwargs.get('seed_sequence', None)
        sample_length = kwargs.get('sample_length', 100)

        assert seed_sequence is not None, "Need a seed sample."
        batch_dimension = seed_sequence.ndim == 3
        if batch_dimension is False:
            seed_sequence = np.expand_dims(seed_sequence, axis=0)

        # Feed seed sequence and update RNN state.
        if not("state" in self.ops_model_output):
            self.ops_evaluation["state"] = self.rnn_output_state
        model_outputs = self.session.run(self.ops_evaluation, feed_dict={self.pl_inputs: seed_sequence, self.pl_seq_length:np.ones(seed_sequence.shape[0])*seed_sequence.shape[1]})

        # Get the last step.
        last_step = model_outputs['sample'][:, -1:, :]
        model_outputs = self.sample_function(last_step, model_outputs['state'], sample_length)

        if batch_dimension is False:
            model_outputs["sample"] = model_outputs["sample"][0]

        return model_outputs

    def sample_function(self, current_input, previous_state, sample_length):
        """
        Auxiliary method to draw sequence of samples in auto-regressive fashion.
        Args:
        Returns:
            Synthetic samples as numpy array (batch_size, sample_length, feature_size)
        """
        # TODO accumulate other evaluation results.
        sequence = current_input.copy()
        num_samples = sequence.shape[0]
        for step in range(sample_length):
            feed_dict = {self.pl_inputs     : sequence[:, -1:, :],
                         self.initial_states: previous_state,
                         self.pl_seq_length : np.ones(num_samples)}
            model_outputs = self.session.run(self.ops_evaluation, feed_dict=feed_dict)
            previous_state = model_outputs['state']

            sequence = np.concatenate([sequence, model_outputs['sample']], axis=1)
        return {"sample": sequence[:, -sample_length:]}


class VRNN(BaseRNN):
    """
    Variational RNN model.
    """
    def __init__(self, config, session, reuse, mode, placeholders, input_dims, target_dims, **kwargs):
        super(VRNN, self).__init__(config, session, reuse, mode, placeholders, input_dims, target_dims, **kwargs)

        self.latent_size = self.config.get('latent_size')

        self.vrnn_cell_constructor = getattr(sys.modules[__name__], self.config.get('vrnn_cell_cls'))
        # TODO: Create a dictionary just for cell arguments.
        self.vrnn_cell_args = copy.deepcopy(config.config)
        self.vrnn_cell_args['input_dims'] = self.input_dims
        self.vrnn_cell_args['output_layer'] = self.output_layer_config

        kld_weight = self.config.get('kld_weight', 0.5)
        if isinstance(kld_weight, dict) and self.global_step:
            self.kld_weight = get_decay_variable(global_step=self.global_step, config=kld_weight, name="kld_weight")
        else:
            self.kld_weight = kld_weight
        if not self.is_training:
            self.kld_weight = 1.0

    def build_cell(self):
        self.cell = self.vrnn_cell_constructor(reuse=self.reuse, mode=self.mode, config=self.vrnn_cell_args, sample_fn=self.sample_fn_tf)

        assert isinstance(self.cell, VRNNCell), "Cell object must be an instance of VRNNCell for VRNN model."
        self.initial_states = self.cell.zero_state(batch_size=self.batch_size, dtype=tf.float32)

    def build_output_layer(self):
        # These are the predefined vrnn cell outputs.
        vrnn_model_out_keys = [C.Q_MU, C.Q_SIGMA, C.P_MU, C.P_SIGMA]
        vrnn_model_out_keys.extend(self.output_layer_config['out_keys'])

        # Assign model outputs.
        for out_key, out_op in zip(vrnn_model_out_keys, self.rnn_outputs):
            self.ops_model_output[out_key] = out_op

        self.ops_evaluation[C.P_MU] = self.ops_model_output[C.P_MU]
        self.ops_evaluation[C.P_SIGMA] = self.ops_model_output[C.P_SIGMA]
        self.ops_evaluation[C.Q_MU] = self.ops_model_output[C.Q_MU]
        self.ops_evaluation[C.Q_SIGMA] = self.ops_model_output[C.Q_SIGMA]
        self.ops_evaluation['state'] = self.rnn_output_state

        num_entries = tf.cast(tf.reduce_sum(self.seq_loss_mask), tf.float32)*tf.cast(tf.shape(self.ops_model_output[C.OUT_MU])[-1], tf.float32)
        if C.OUT_MU in self.ops_model_output:
            self.ops_scalar_summary["mean_out_mu"] = tf.reduce_sum(self.ops_model_output[C.OUT_MU]*self.seq_loss_mask)/num_entries
        if C.OUT_SIGMA in self.ops_model_output:
            self.ops_scalar_summary["mean_out_sigma"] = tf.reduce_sum(self.ops_model_output[C.OUT_SIGMA]*self.seq_loss_mask) / num_entries

        self.output_sample = self.sample_fn_tf(self.ops_model_output)
        self.input_sample = self.pl_inputs
        self.ops_evaluation['sample'] = self.output_sample

        num_entries = tf.cast(tf.reduce_sum(self.seq_loss_mask), tf.float32)*tf.cast(tf.shape(self.ops_model_output[C.P_MU])[-1], tf.float32)
        self.ops_scalar_summary["mean_p_sigma"] = tf.reduce_sum(self.ops_model_output[C.P_SIGMA]*self.seq_loss_mask) / num_entries
        self.ops_scalar_summary["mean_q_sigma"] = tf.reduce_sum(self.ops_model_output[C.Q_SIGMA]*self.seq_loss_mask) / num_entries
        self.ops_scalar_summary["mean_q_mu"] = tf.reduce_sum(self.ops_model_output[C.Q_MU]*self.seq_loss_mask)/num_entries
        self.ops_scalar_summary["mean_p_mu"] = tf.reduce_sum(self.ops_model_output[C.P_MU]*self.seq_loss_mask)/num_entries

    def build_loss_terms(self):
        """
        Builds loss terms.
        """
        super(VRNN, self).build_loss_terms()

        loss_key = 'loss_kld'
        if loss_key not in self.ops_loss:
            with tf.name_scope('kld_loss'):
                # KL-Divergence.
                self.ops_loss['loss_kld'] = self.kld_weight*self.reduce_loss_fn(
                    self.seq_loss_mask*tf_loss.kld_normal_isotropic(self.ops_model_output[C.Q_MU],
                                                                    self.ops_model_output[C.Q_SIGMA],
                                                                    self.ops_model_output[C.P_MU],
                                                                    self.ops_model_output[C.P_SIGMA], reduce_sum=False))

    def build_summary_plots(self):
        """
        Creates scalar summaries for loss plots. Iterates through `ops_loss` member and create a summary entry.

        If the model is in `validation` mode, then we follow a different strategy. In order to have a consistent
        validation report over iterations, we first collect model performance on every validation mini-batch
        and then report the average loss. Due to tensorflow's lack of loss averaging ops, we need to create
        placeholders per loss to pass the average loss.
        """
        super(VRNN, self).build_summary_plots()

        # Create summaries to visualize distribution of latent variables.
        if self.config.get('tensorboard_verbose', 0) > 1:
            set_of_graph_nodes = [C.Q_MU, C.Q_SIGMA, C.P_MU, C.P_SIGMA, C.OUT_MU, C.OUT_SIGMA]
            for out_key in set_of_graph_nodes:
                tf.summary.histogram(out_key, self.ops_model_output[out_key], collections=[self.mode+'_summary_plot', self.mode+'_stochastic_variables'])

    def reconstruct(self, **kwargs):
        """
        Predicts the next step by using previous ground truth steps.
        Args:
            **kwargs:
        Returns:
            Predictions of next steps (batch_size, input_seq_len, feature_size)
        """
        input_sequence = kwargs.get('input_sequence', None)
        target_sequence = kwargs.get('target_sequence', None)

        assert input_sequence is not None, "Need an input sample."
        batch_dimension = input_sequence.ndim == 3
        if batch_dimension is False:
            input_sequence = np.expand_dims(input_sequence, axis=0)

        if not("state" in self.ops_evaluation):
            self.ops_evaluation["state"] = self.rnn_output_state

        sample_length = input_sequence.shape[1]
        feed_dict = {self.pl_inputs: input_sequence, self.pl_seq_length:np.ones(1)*sample_length}

        if target_sequence is not None:
            if batch_dimension is False:
                target_sequence = np.expand_dims(target_sequence, axis=0)

            if "loss" not in self.ops_evaluation:
                self.ops_evaluation['loss'] = self.ops_loss

            feed_dict[self.pl_targets] = target_sequence

        model_outputs = self.session.run(self.ops_evaluation, feed_dict)
        if "loss" in model_outputs:
            self.log_loss(model_outputs['loss'])

        if batch_dimension is False:
            model_outputs["sample"] = model_outputs["sample"][0]

        return model_outputs

    def sample(self, **kwargs):
        """
        Sampling function. Since model has different graphs for sampling and evaluation modes, a seed state must be
        given in order to predict future steps. Otherwise, a sample will be synthesized randomly.

        Args:
            **kwargs:
        """
        assert self.is_sampling, "The model must be in sampling mode."

        seed_state = kwargs.get('seed_state', None)
        sample_length = kwargs.get('sample_length', 100)
        batch_dimension = False

        if not("state" in self.ops_evaluation):
            self.ops_evaluation["state"] = self.rnn_output_state

        dummy_x = np.zeros((1, sample_length, sum(self.input_dims)))

        # Feed seed sequence and update RNN state.
        feed = {self.pl_inputs    : dummy_x,
                self.pl_seq_length: np.ones(1)*sample_length}
        if seed_state is not None:
            feed[self.initial_states] = seed_state

        model_outputs = self.session.run(self.ops_evaluation, feed_dict=feed)

        if batch_dimension is False:
            model_outputs["sample"] = model_outputs["sample"][0]

        return model_outputs
