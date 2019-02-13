import numpy as np


class Constants(object):
    SEED = 1234

    # Run modes.
    TRAIN = 'training'
    TEST = 'test'
    VALID = 'validation'
    EVAL = 'evaluation'
    SAMPLE = 'sampling'

    # RNN cells
    GRU = 'gru'
    LSTM = 'lstm'
    BLSTM = 'blstm'
    LayerNormLSTM = 'LayerNormBasicLSTMCell'

    # Activation functions
    RELU = 'relu'
    ELU = 'elu'
    SIGMOID = 'sigmoid'
    SOFTPLUS = 'softplus'
    TANH = 'tanh'
    SOFTMAX = 'softmax'
    LRELU = 'lrelu'
    CLRELU = 'clrelu'  # Clamped leaky relu.

    # Losses
    NLL_BERNOULLI = 'nll_bernoulli'
    NLL_NORMAL = 'nll_normal'
    NLL_BINORMAL = 'nll_binormal'
    NLL_GMM = 'nll_gmm'
    NLL_BIGMM = 'nll_bigmm'
    NLL_CENT = 'nll_cent'  # Cross-entropy.
    NLL_CENT_BINARY = 'nll_cent_binary'  # Cross-entropy for binary outputs.
    KLD = 'kld'
    L1 = 'l1'
    MSE = 'mse'

    # Model output names
    OUT_MU = 'out_mu'
    OUT_SIGMA = 'out_sigma'
    OUT_RHO = 'out_rho'
    OUT_COEFFICIENT = 'out_coefficient'  # For GMM outputs only.
    OUT_BINARY = 'out_binary'  # For binary outputs and bernoulli loss

    # Suffix for output names.
    SUF_MU = '_mu'
    SUF_SIGMA = '_sigma'
    SUF_RHO = '_rho'
    SUF_COEFFICIENT = '_coefficient'  # For GMM outputs only.
    SUF_BINARY = '_binary'  # For binary outputs and bernoulli loss
    SUF_CENT = '_logit'  # For cross-entropy loss

    # Reduce function types
    R_MEAN_STEP = 'mean_step_loss'  # Take average of average step loss per sample over batch. Uses sequence length.
    R_MEAN_SEQUENCE = 'mean_sequence_loss'  # Take average of sequence loss (summation of all steps) over batch. Uses sequence length.
    R_MEAN = 'mean'  # Take mean of the whole tensor.
    R_SUM = 'sum'  # Take mean of the whole tensor.
    B_MEAN_STEP = 'batch_mean_step_loss'  # Keep the loss per sample. Uses sequence length.
    R_IDENTITY = 'identity'

    # Models
    MODEL_RNN = 'rnn'
    MODEL_TCN = 'tcn'  # Temporal Convolutional Network (i.e. Wavenet Model)
    MODEL_STCN = 'stcn'  # Stochastic Temporal Convolutional Network
    MODEL_VRNN = 'vrnn'  # Variational Recurrent Neural Network.

    # Digital Ink Datasets
    IAMONDB = "iam"
    DEEPWRITING = "dw"

    # Speech datasets
    TIMIT = "timit"
    BLIZZARD = "blizzard"

    # Dataset I/O keys for TF placeholders.
    PL_INPUT = "pl_input"
    PL_TARGET = "pl_target"
    PL_SEQ_LEN = "pl_seq_len"
    PL_IDX = "pl_idx"

    # Latent components.
    Q_MU = 'q_mu'
    Q_SIGMA = 'q_sigma'
    P_MU = 'p_mu'
    P_SIGMA = 'p_sigma'
    Z_LATENT = 'z_latent'
    Q_PI = 'q_pi'
    P_PI = 'p_pi'
    LATENT_Q = 'q'  # approximate_posterior
    LATENT_P = 'p'  # prior

    # Preprocessing operations.
    PP_SHIFT = "pp_shift"
    PP_ZERO_MEAN_NORM = "pp_zero_mean_normalization"
    PP_ZERO_MEAN_NORM_SEQ = "pp_zero_mean_norm_seq_stats"
    PP_ZERO_MEAN_NORM_ALL = "pp_zero_mean_norm_all_stats"

    # Latent layers.
    LATENT_GAUSSIAN = "latent_gaussian"
    LATENT_LADDER_GAUSSIAN = "latent_ladder_gaussian"

    # Layer types.
    LAYER_FC = "fc"
    LAYER_RNN = "rnn"
    LAYER_TCN = "tcn"  # Causal convolutional layer.
    LAYER_CONV1 = "conv1"  # 1 dimensional convolution.

    DECAY_PC = "piecewise_constant"
    DECAY_EXP = "exponential_decay"
    DECAY_LINEAR = "linear_decay"

    RGB_COLORS = [np.array((0, 13, 53)), np.array((0, 91, 149)), np.array((171, 19, 19)),
                  np.array((254, 207, 103)), np.array((153, 104, 129)), np.array((255, 165, 120)),
                  np.array((70, 163, 203)), np.array((194, 34, 80)), np.array((63, 140, 115)), np.array((255, 119, 0))]
