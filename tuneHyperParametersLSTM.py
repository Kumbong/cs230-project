import os
import numpy as np
import pickle
import platform
import multiprocessing
import tensorflow as tf

from mySettings import get_lstm_tuner_settings
from modeldefs.myModelsHyperParameters import get_lstm_model
from utils.myDataGenerator import lstmDataGenerator
from utils.utilities import getAllMarkers, rotateArray, getMarkers_lowerExtremity_angularconstraints

import keras_tuner
from keras_tuner.tuners import RandomSearch
import argparse

parser = argparse.ArgumentParser(description='Process some integers.')
parser.add_argument('--case', type=int, help="Setting case to execute")
args = parser.parse_args()

if args.case == None:
    raise Exception("Need a case number")

# %% User inputs.
# Select case you want to train, see mySettings for case-specific settings.
case = args.case

runParameterTuning = True
printTunerSummary = True
saveTunedModel = True
runTunedModelRetraining = True

# %% Paths
if platform.system() == 'Linux':
    # To use docker.
    pathMain = os.getcwd()
else:
    pathMain = os.getcwd()
pathData = os.path.join(pathMain, "Data")
pathData_all = os.path.join(pathData, "data_CS230_small")
pathTrainedModels = os.path.join(pathMain, "trained_models_LSTM")
os.makedirs(pathTrainedModels, exist_ok=True)
pathParameterTuning = os.path.join(
    pathMain, "trained_models_hyperparameter_tuning_LSTM")
pathParameterTuningModels = os.path.join(pathParameterTuning,
                                         "Models", str(case))
os.makedirs(pathParameterTuningModels, exist_ok=True)
pathCModel = os.path.join(pathParameterTuningModels, "")

# %% Load settings.
settings = get_lstm_tuner_settings(case)
augmenter_type = settings["augmenter_type"]
poseDetector = settings["poseDetector"]
idxDatasets = settings["idxDatasets"]
scaleFactors = settings["scaleFactors"]
nEpochs = settings["nEpochs"]
batchSize = settings["batchSize"]
idxFold = settings["idxFold"]
mean_subtraction = settings["mean_subtraction"]
std_normalization = settings["std_normalization"]
units_h = settings["units_h"]
layer_h = settings["layer_h"]

# Noise.
noise_bool = False  # default
noise_magnitude = 0  # default
noise_type = ''  # default
if "noise_magnitude" in settings:
    noise_magnitude = settings["noise_magnitude"]
    if noise_magnitude > 0:
        noise_bool = True
    noise_type = 'per_timestep'
    if 'noise_type' in settings:
        noise_type = settings["noise_type"]
# Settings for hyperparameter tuning.
max_trials = settings["max_trials"]
executions_per_trial = settings["executions_per_trial"]
nEpochsBest = settings["nEpochsBest"]

# Hyperparamters to tune.
learning_r = settings["learning_r"]
lambda_1 = settings["lambda_1"]
lambda_2 = settings["lambda_2"]
lambda_3 = settings["lambda_3"]
# Rotation.
nRotations = 1
if "nRotations" in settings:
    nRotations = settings["nRotations"]
rotations = [i*360/nRotations for i in range(0, nRotations)]

# Bidirectional LSTM.
# https://keras.io/api/layers/recurrent_layers/bidirectional/
bidirectional = False
if 'bidirectional' in settings:
    bidirectional = settings["bidirectional"]
# %% Fixed settings (no need to change that).
featureHeight = True
featureWeight = True
marker_dimensions = ["x", "y", "z"]
nDim = len(marker_dimensions)
fc = 60  # sampling frequency (Hz)
desired_duration = 0.5  # (s)
desired_nFrames = int(desired_duration * fc)

# Use multiprocessing (only working on Linux apparently).
# https://keras.io/api/models/model_training_apis/
if platform.system() == 'Linux':
    use_multiprocessing = True
    # Use all but 2 thread
    nWorkers = multiprocessing.cpu_count() - 2
else:
    # Not supported on Windows
    use_multiprocessing = False
    nWorkers = 1

length_constraints = None
angular_constraints= None

# %% Helper indices (no need to change that).
# Get indices features/responses based on augmenter_type and poseDetector.
feature_markers_all, response_markers_all = getAllMarkers()
if augmenter_type == 'fullBody':
    if poseDetector == 'OpenPose':
        from utils.utilities import getOpenPoseMarkers_fullBody
        _, _, idx_in_all_feature_markers, idx_in_all_response_markers = (
            getOpenPoseMarkers_fullBody())
    elif poseDetector == 'mmpose':
        from utils.utilities import getMMposeMarkers_fullBody
        _, _, idx_in_all_feature_markers, idx_in_all_response_markers = (
            getMMposeMarkers_fullBody())
    else:
        raise ValueError('poseDetector not recognized')
elif augmenter_type == 'lowerExtremity':
    if poseDetector == 'OpenPose':
        from utils.utilities import getOpenPoseMarkers_lowerExtremity, getMarkers_lowerExtremity_constraints, translateConstraints
        _, response_markers, idx_in_all_feature_markers, idx_in_all_response_markers = (
            getOpenPoseMarkers_lowerExtremity())
        length_constraints = translateConstraints(getMarkers_lowerExtremity_constraints(), response_markers)
        angular_constraints = getMarkers_lowerExtremity_angularconstraints()
    elif poseDetector == 'mmpose':
        from utils.utilities import getMMposeMarkers_lowerExtremity
        _, _, idx_in_all_feature_markers, idx_in_all_response_markers = (
            getMMposeMarkers_lowerExtremity())
    else:
        raise ValueError('poseDetector not recognized')
elif augmenter_type == 'upperExtremity_pelvis':
    from utils.utilities import getMarkers_upperExtremity_pelvis
    _, _, idx_in_all_feature_markers, idx_in_all_response_markers = (
        getMarkers_upperExtremity_pelvis())
elif augmenter_type == 'upperExtremity_noPelvis':
    from utils.utilities import getMarkers_upperExtremity_noPelvis
    _, _, idx_in_all_feature_markers, idx_in_all_response_markers = (
        getMarkers_upperExtremity_noPelvis())
else:
    raise ValueError('augmenter_type not recognized')
# Each marker has 3 dimensions.
idx_in_all_features = []
for idx in idx_in_all_feature_markers:
    idx_in_all_features.append(idx*3)
    idx_in_all_features.append(idx*3+1)
    idx_in_all_features.append(idx*3+2)
idx_in_all_responses = []
for idx in idx_in_all_response_markers:
    idx_in_all_responses.append(idx*3)
    idx_in_all_responses.append(idx*3+1)
    idx_in_all_responses.append(idx*3+2)

# Additional features (height and weight).
nAddFeatures = 0
nFeature_markers = len(idx_in_all_feature_markers)*nDim
nResponse_markers = len(idx_in_all_response_markers)*nDim
if featureHeight:
    nAddFeatures += 1
    idxFeatureHeight = len(feature_markers_all)*nDim
    idx_in_all_features.append(idxFeatureHeight)
if featureWeight:
    nAddFeatures += 1
    if featureHeight:
        idxFeatureWeight = len(feature_markers_all)*nDim + 1
    else:
        idxFeatureWeight = len(feature_markers_all)*nDim
    idx_in_all_features.append(idxFeatureWeight)

# %% Partition (select data for training, validation, and test).
datasetName = ' '.join([str(elem) for elem in idxDatasets])
datasetName = datasetName.replace(' ', '_')
scaleFactorName = ' '.join([str(elem) for elem in scaleFactors])
scaleFactorName = scaleFactorName.replace(' ', '_')
scaleFactorName = scaleFactorName.replace('.', '')
partitionName = ('{}_{}_{}_{}'.format(augmenter_type, poseDetector,
                                      datasetName, scaleFactorName))
pathPartition = os.path.join(
    pathData_all, 'partition_{}.npy'.format(partitionName))
if not os.path.exists(pathPartition):
    print('Computing partition')
    # Load splitting settings.
    subjectSplit = np.load(os.path.join(pathData, 'subjectSplit.npy'),
                           allow_pickle=True).item()
    # Load infoData dict.
    infoData = np.load(os.path.join(pathData_all, 'infoData.npy'),
                       allow_pickle=True).item()
    # Get partition.
    from utils.utilities import getPartition
    partition = getPartition(idxDatasets, scaleFactors, infoData,
                             subjectSplit, idxFold)
    # Save partition.
    np.save(pathPartition, partition)
else:
    # Load partition.
    partition = np.load(pathPartition, allow_pickle=True).item()
# %% Data processing: add noise and compute mean and standard deviation.
pathMean = os.path.join(pathData_all, 'mean_{}_{}_{}_{}.npy'.format(
    partitionName, noise_type, noise_magnitude, nRotations))
pathSTD = os.path.join(pathData_all, 'std_{}_{}_{}_{}.npy'.format(
    partitionName, noise_type, noise_magnitude, nRotations))
if not os.path.exists(pathMean) and not os.path.exists(pathSTD):
    print('Computing mean and standard deviation')
    # Instead of accumulating data to compute mean and std, we compute them
    # on the fly so that we do not have to deal with too large matrices.
    existingAggregate = (0, 0, 0)
    from utils.utilities import update
    from utils.utilities import finalize
    for count, idx in enumerate(partition['train']):
        c_features_all = np.load(os.path.join(
            pathData_all, "feature_{}.npy".format(idx)))
        # Select features.
        c_features = c_features_all[:, idx_in_all_features]
        # Apply rotations.
        for rotation in rotations:
            c_features_xyz_rot = rotateArray(
                c_features[:, :nFeature_markers], 'y', rotation)
            c_features_rot = np.concatenate(
                (c_features_xyz_rot, c_features[:, nFeature_markers:]), axis=1)
            # Add noise to the training data.
            # Here we adjust the np.random.seed at each iteration such that we
            # can use the exact same noise in the data generator.
            if noise_magnitude > 0:
                if noise_type == "per_timestep":
                    np.random.seed(idx)
                    noise = np.zeros((desired_nFrames,
                                      nFeature_markers+nAddFeatures))
                    noise[:, :nFeature_markers] = np.random.normal(
                        0, noise_magnitude,
                        (desired_nFrames, nFeature_markers))
                    c_features += noise
                else:
                    raise ValueError("Only per_timestep noise type supported")
            if not c_features.shape[0] == 30:
                raise ValueError("Dimension features and responses are wrong")
            # Compute mean and std iteratively.
            for c_s in range(c_features.shape[0]):
                existingAggregate = update(
                    existingAggregate, c_features[c_s, :])
    # Compute final mean and standard deviation.
    (features_mean, features_variance, _) = finalize(existingAggregate)
    features_std = np.sqrt(features_variance)
    np.save(pathMean, features_mean)
    np.save(pathSTD, features_std)
else:
    features_mean = np.load(pathMean)
    features_std = np.load(pathSTD)

# %% Initialize data generators.
# https://stanford.edu/~shervine/blog/keras-how-to-generate-data-on-the-fly
params = {'dim_f': (desired_nFrames, nFeature_markers+nAddFeatures),
          'dim_r': (desired_nFrames, nResponse_markers),
          'batch_size': batchSize,
          'shuffle': True,
          'noise_bool': noise_bool,
          'noise_type': noise_type,
          'noise_magnitude': noise_magnitude,
          'mean_subtraction': mean_subtraction,
          'std_normalization': std_normalization,
          'features_mean': features_mean,
          'features_std': features_std,
          'idx_features': idx_in_all_features,
          'idx_responses': idx_in_all_responses,
          'rotations': rotations}
train_generator = lstmDataGenerator(partition['train'], pathData_all, **params)
val_generator = lstmDataGenerator(partition['val'], pathData_all, **params)

# %% Parameter tuning.
tuner = RandomSearch(
   get_lstm_model(input_dim=nFeature_markers+nAddFeatures, output_dim=nResponse_markers,
                       layer_h=layer_h, units_h=units_h, learning_r=learning_r, loss_f='output_constr',
                       batch_size = batchSize,desired_nFrames=desired_nFrames,bidirectional=bidirectional, length_constraints=length_constraints, angular_constraints=angular_constraints, lambda_1 = lambda_1, lambda_2 = lambda_2, lambda_3 = lambda_3)
,
    objective=keras_tuner.Objective("val_root_mean_squared_error",
                                    direction="min"),
    max_trials=max_trials,
    executions_per_trial=executions_per_trial,
    directory=pathParameterTuning,
    project_name=str(case),
    overwrite=False)

if runParameterTuning:
    tuner.search_space_summary()
    # Unfortunately, there is an error w/ using early stopping + tuner + generators
    # https://github.com/keras-team/keras-tuner/issues/688
    tuner.search(train_generator,
                 epochs=nEpochs,
                 verbose=2,
                 validation_data=val_generator,
                 workers=nWorkers,
                 use_multiprocessing=use_multiprocessing)

if printTunerSummary:
    tuner.results_summary(num_trials=3)

# Extract best hyperparameters and build model.
# To get second best: hps = tuner.oracle.get_best_trials(num_trials=2)[1].hyperparameters
hps = tuner.oracle.get_best_trials(num_trials=1)[0].hyperparameters

hyperModel = get_lstm_model(input_dim=nFeature_markers+nAddFeatures, output_dim=nResponse_markers,
                       layer_h=layer_h, units_h=units_h, learning_r=learning_r, loss_f='output_constr',
                       batch_size = batchSize,desired_nFrames=desired_nFrames, bidirectional=bidirectional, length_constraints=length_constraints, angular_constraints=angular_constraints, lambda_1 = lambda_1, lambda_2 = lambda_2, lambda_3 = lambda_3)

model = hyperModel.build(hps)
model.summary()
# Save model
if saveTunedModel:
    model_json = model.to_json()
    with open(pathCModel + str(case) + "_model.json", "w") as json_file:
        json_file.write(model_json)

# %% Train best tuned model on more epochs.
if runTunedModelRetraining:
    callback = tf.keras.callbacks.EarlyStopping(
        monitor='val_loss', patience=3, verbose=1, mode="auto",
        restore_best_weights=True)
    history = model.fit(
        train_generator, validation_data=val_generator,
        epochs=nEpochsBest, verbose=2, workers=nWorkers,
        use_multiprocessing=use_multiprocessing, callbacks=[callback])
    # Save weights
    model.save_weights(pathCModel + str(case) + "_weights.h5")
    # Save history
    with open(pathCModel + str(case) + "_history", 'wb') as file_pi:
        pickle.dump(history.history, file_pi)
    # Save mean and std used for data processing.
    if mean_subtraction:
        np.save(os.path.join(pathParameterTuningModels,
                             str(case) + "_trainFeatures_mean.npy"),
                features_mean)
    if std_normalization:
        np.save(os.path.join(pathParameterTuningModels,
                             str(case) + "_trainFeatures_std.npy"),
                features_std)
    # Save learning rate
    import tensorflow.keras.backend as K
    optimal_learning_rate = K.eval(model.optimizer.lr)
    np.save(os.path.join(pathParameterTuningModels,
                         str(case) + "_learningRate.npy"),
            optimal_learning_rate)
