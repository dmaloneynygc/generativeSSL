from __future__ import absolute_import 
from __future__ import division
from __future__ import print_function

import sys, os, pdb

import numpy as np
import utils.dgm as dgm

import tensorflow as tf
from tensorflow.contrib.tensorboard.plugins import projector

""" Super class for deep generative models """

class model(object):

    def __init__(self, Z_DIM=2, LEARNING_RATE=0.005, NUM_HIDDEN=[4], TYPE_PX='Gaussian', NONLINEARITY=tf.nn.relu, temperature_epochs=None, start_temp=None,  NUM_EPOCHS=75, Z_SAMPLE=1, BINARIZE=False, logging=False, alpha=0.1):

	self.Z_DIM = Z_DIM                       # number of labeled dimensions
	self.NUM_HIDDEN = NUM_HIDDEN             # network architectures
	self.NONLINEARITY = NONLINEARITY         # activation function
	self.TYPE_PX = TYPE_PX                   # likelihood for inputs
	self.temp_epochs = temperature_epochs    # length of warmup period
	self.start_temp = start_temp             # starting temperature for KL divergence
	self.NUM_EPOCHS = NUM_EPOCHS             # Number of training epochs
	self.Z_SAMPLES = Z_SAMPLE                # MC estimation with Z
	self.BINARIZE = BINARIZE                 # binarize the data
	self.LOGGING = False                     # log with tensorboard
	self.alpha = alpha                       # temporary
	self.name = 'model'                      # model name

	# Set learning rate
	self.global_step = tf.Variable(0, trainable=False, name='global_step')
	if len(LEARNING_RATE)==1:
	    self.lr = LEARNING_RATE = LEARNING_RATE[0]
	else:
	    start_lr = LEARNING_RATE[0]
	    self.lr = tf.train.exponential_decay(start_lr, self.global_step, LEARNING_RATE[1], 0.96) 

    

    def _compute_logpx(self, x, z, y=None):
        """ compute the likelihood of every element in x under p(x|z) """
        if self.TYPE_PX == 'Gaussian':
            mean, log_var = dgm._forward_pass_Gauss(z,self.Pz_x, self.NUM_HIDDEN, self.NONLINEARITY)
            return dgm._gauss_logp(x, mean, tf.exp(log_var))
        elif self.TYPE_PX == 'Bernoulli':
            pi = dgm._forward_pass_Bernoulli(z, self.Pz_x, self.NUM_HIDDEN, self.NONLINEARITY)
            return tf.reduce_sum(tf.add(x * tf.log(1e-10 + pi),  (1-x) * tf.log(1e-10 + 1 - pi)), axis=1)


    def _compute_logpy(self, y, x, z):
        """ compute the likelihood of every element in y under p(y|x,z) """
        h = tf.concat([x,z], axis=1)
        y_ = dgm._forward_pass_Cat_logits(h, self.Pzx_y, self.NUM_HIDDEN, self.NONLINEARITY)
        return -tf.nn.softmax_cross_entropy_with_logits(labels=y, logits=y_)


    def _weight_regularization(self):
	weights = [V for V in tf.trainable_variables() if 'W' in V.name]
	return np.sum([tf.nn.l2_loss(w) for w in weights])	


    def _binarize(self, x):
	return np.random.binomial(1,x)

    def _set_schedule(self):
	if not self.temp_epochs:
	    self.schedule = np.ones((self.NUM_EPOCHS, 1)).astype('float32')
	else:
	    warmup = np.expand_dims(np.linspace(self.start_temp, 1.0, self.temp_epochs),1)
	    plateau = np.ones((self.NUM_EPOCHS-self.temp_epochs,1))
	    self.schedule = np.ravel(np.vstack((warmup, plateau))).astype('float32')
	self.beta = tf.Variable(self.schedule[0], trainable=False, name='beta')


    def _compute_loss_weights(self):
        """ Compute scaling weights for the loss function """
        self.labeled_weight = tf.cast(tf.divide(self.N , tf.multiply(self.NUM_LABELED, self.LABELED_BATCH_SIZE)), tf.float32)
        self.unlabeled_weight = tf.cast(tf.divide(self.N , tf.multiply(self.NUM_UNLABELED, self.UNLABELED_BATCH_SIZE)), tf.float32)



    def _process_data(self, data):
        """ Extract relevant information from data_gen """
        self.N = data.N
        self.TRAINING_SIZE = data.TRAIN_SIZE           # training set size
        self.TEST_SIZE = data.TEST_SIZE                # test set size
        self.NUM_LABELED = data.NUM_LABELED            # number of labeled instances
        self.NUM_UNLABELED = data.NUM_UNLABELED        # number of unlabeled instances
        self.X_DIM = data.INPUT_DIM                    # input dimension     
        self.NUM_CLASSES = data.NUM_CLASSES            # number of classes
        self.alpha = self.alpha * self.NUM_LABELED     # weighting for additional term
        self.data_name = data.NAME                     # dataset being used   
        self._allocate_directory()                     # logging directory


    def _create_placeholders(self):
        """ Create input/output placeholders """
	if self.name != 'vae':
            self.x_labeled = tf.placeholder(tf.float32, shape=[self.LABELED_BATCH_SIZE, self.X_DIM], name='labeled_input')
            self.x_unlabeled = tf.placeholder(tf.float32, shape=[self.UNLABELED_BATCH_SIZE, self.X_DIM], name='unlabeled_input')
            self.labels = tf.placeholder(tf.float32, shape=[self.LABELED_BATCH_SIZE, self.NUM_CLASSES], name='labels')
	else:
	    self.x_batch = tf.placeholder(tf.float32, shape=[self.BATCH_SIZE, self.X_DIM], name='x_batch')
        self.x_train = tf.placeholder(tf.float32, shape=[self.TRAINING_SIZE, self.X_DIM], name='x_train')
        self.y_train = tf.placeholder(tf.float32, shape=[self.TRAINING_SIZE, self.NUM_CLASSES], name='y_train')
        self.x_test = tf.placeholder(tf.float32, shape=[self.TEST_SIZE, self.X_DIM], name='x_test')
        self.y_test = tf.placeholder(tf.float32, shape=[self.TEST_SIZE, self.NUM_CLASSES], name='y_test')


    def _create_summaries(self, L_l, L_u, L_e):
	with tf.name_scope("summaries_elbo"):
            tf.summary.scalar("ELBO", self.loss)
            tf.summary.scalar("Labeled Loss", L_l)
            tf.summary.scalar("Unlabeled Loss", L_u)
            tf.summary.scalar("Additional Penalty", L_e)
            self.summary_op_elbo = tf.summary.merge_all()


        with tf.name_scope("summaries_accuracy"):
            train_summary = tf.summary.scalar("Train Accuracy", self.train_acc)
            test_summary = tf.summary.scalar("Test Accuracy", self.test_acc)
            self.summary_op_acc = tf.summary.merge([train_summary, test_summary])



    def _printing_feed_dict(self, Data, x, y):
	return {self.x_train:Data.data['x_train'], self.y_train:Data.data['y_train'],
                self.x_test:Data.data['x_test'], self.y_test:Data.data['y_test'],
                self.x_labeled:x, self.labels:y}

    def _print_verbose0(self, epoch, step, total_loss, l_l, l_u, l_e, acc_train, acc_test):
        print("Epoch {}: Total:{:5.1f}, Labeled:{:5.1f}, unlabeled:{:5.1f}, "
              "Additional:{:5.1f}, Training: {:5.3f}, Test: {:5.3f}".format(epoch,
                                                                            total_loss/step, l_l/step,
                                                                            l_u/step,l_e/step,
                                                                            acc_train, acc_test))   


    def _print_verbose1(self,epoch, fd,sess):
	zm_test, zlv_test, z_test = self._sample_Z(self.x_test,self.y_test,1)
	zm_train, zlv_train, z_train = self._sample_Z(self.x_train,self.y_train,1)
	lpx_test, lpx_train,klz_test,klz_train, acc_train, acc_test = sess.run([self._compute_logpx(self.x_test, z_test), 
                                                                  self._compute_logpx(self.x_train, z_train),
                                                                  dgm._gauss_kl(zm_test, tf.exp(zlv_test)),
                                                                  dgm._gauss_kl(zm_train, tf.exp(zlv_train)),
                                                                  self.train_acc, self.test_acc], feed_dict=fd)
	print('Epoch: {}, logpx: {:5.3f}, klz: {:5.3f}, Train: {:5.3f}, Test: {:5.3f}'.format(epoch, np.mean(lpx_train), np.mean(klz_train), acc_train, acc_test ))


    def _save_model(self, saver, session, step, max_val, curr_val):
	saver.save(session, self.ckpt_dir, global_step=step+1)
	if curr_val > max_val:
	    saver.save(session, self.ckpt_best, global_step=step+1)

    def _allocate_directory(self):
        self.LOGDIR = 'graphs/'+self.name+'-' +self.data_name+'-'+str(self.NUM_LABELED)+'/'
        self.ckpt_dir = './ckpt/'+self.name+'-'+self.data_name+'-'+str(self.NUM_LABELED) + '/'
        self.ckpt_best = './ckpt/'+self.name+'-'+self.data_name+'-'+str(self.NUM_LABELED) + '-best/'
        if not os.path.isdir(self.ckpt_dir):
            os.mkdir(self.ckpt_dir)
        if not os.path.isdir(self.ckpt_best):
            os.mkdir(self.ckpt_best)

