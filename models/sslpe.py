from __future__ import absolute_import
from __future__ import division 
from __future__ import print_function

from models.model import model

import sys, os, pdb

import numpy as np
import utils.dgm as dgm 

import tensorflow as tf
from tensorflow.contrib.tensorboard.plugins import projector


""" 
Implementation of semi-supervised DGMs from Gordon and Hernandez-Lobato (2017): p(z) * p(x|z) * p(y|x,z) 
Inference network: q(z,y|x) = q(y|x) * q(z|y,x) 
"""

class sslpe(model):
   
    def __init__(self, n_x, n_y, n_z=2, n_hid=[4], alpha=0.1, x_dist='Gaussian', nonlinearity=tf.nn.relu, batchnorm=False, l2_reg=0.3, mc_samples=1,ckpt=None):
	
	super(sslpe, self).__init__(n_x, n_y, n_z, n_hid, x_dist, nonlinearity, batchnorm, mc_samples, alpha, l2_reg, ckpt)


	""" TODO: add any general terms we want to have here """
	self.name = 'sslpe'

    def build_model(self):
	""" Define model components and variables """
	self.create_placeholders()
	self.initialize_networks()
	## model variables and relations ##
	# infernce #
        self.y_ = dgm.forwardPassCatLogits(self.x, self.qy_x, self.n_hid, self.nonlinearity, self.bn, scope='qy_x', reuse=False)
	self.qz_in = tf.concat([self.x, self.y], axis=-1) 
	self.qz_mean, self.qz_lv, self.z_ = dgm.samplePassGauss(self.qz_in, self.qz_xy, self.n_hid, self.nonlinearity, self.bn, scope='qz_xy', reuse=False)
	# generative #
	self.z_prior = tf.random_normal([100, self.n_z])
	if self.x_dist == 'Gaussian':
	    self.px_mean, self.px_lv, self.x_ = dgm.samplePassGauss(self.z_prior, self.px_z, self.n_hid, self.nonlinearity, self.bn, scope='px_z', reuse=False)
	elif self.x_dist == 'Bernoulli':
	    self.x_ = dgm.forwardPassBernoulli(self.z_prior, self.px_z, self.n_hid, self.nonlinearity, self.bn, scope='px_z', reuse=False)
	self.py_in = tf.concat([self.x_, self.z_prior], axis=-1)
	self.py_ = dgm.forwardPassCat(self.py_in, self.py_xz, self.n_hid, self.nonlinearity, self.bn, scope='py_xz', reuse=False)
	self.predictions = self.predict(self.x)

    def compute_loss(self):
	""" manipulate computed components and compute loss """
	self.elbo_l = tf.reduce_mean(self.labeled_loss(self.x_l, self.y_l))
	self.qy_ll = tf.reduce_mean(self.qy_loss(self.x_l, self.y_l))
	self.elbo_u = tf.reduce_mean(self.unlabeled_loss(self.x_u))
	#weight_priors = self.l2_reg*self.weight_prior()/self.n_train	
	weight_priors = -self.l2_reg*self.weight_regularization()/200	
	return -(self.elbo_l + self.elbo_u + self.alpha * self.qy_ll + weight_priors)

    def labeled_loss(self, x, y):
	""" compute the labeled loss """
	z_m, z_lv, z = self.sample_z(x,y)
	x_ = tf.tile(tf.expand_dims(x, 0), [self.mc_samples, 1,1])
	y_ = tf.tile(tf.expand_dims(y,0),[self.mc_samples,1,1])
	return self.lowerBound(x_, y_, z, z_m, z_lv) 

    def unlabeled_loss(self, x):
	""" compute the unlabeled loss """
	qy_l = dgm.forwardPassCat(x, self.qy_x, self.n_hid, self.nonlinearity, self.bn, scope='qy_x')
	x_r = tf.tile(x, [self.n_y,1])
	y_u = tf.reshape(tf.tile(tf.eye(self.n_y), [1, tf.shape(self.x_u)[0]]), [-1, self.n_y])
	n_u = tf.shape(x)[0] 
	lb_u = tf.transpose(tf.reshape(self.labeled_loss(x_r, y_u), [self.n_y, n_u]))
	lb_u = tf.reduce_sum(qy_l * lb_u, axis=-1)
	qy_entropy = -tf.reduce_sum(qy_l * tf.log(qy_l + 1e-10), axis=-1)
	return lb_u + qy_entropy

    def lowerBound(self, x, y, z, z_m, z_lv):
	""" compute densities and lower bound given all inputs (mc_samps X n_obs X n_dim) """
	l_px = self.compute_logpx(x,z)
	l_py = self.compute_logpy(y, x, z)
	l_pz = dgm.standardNormalLogDensity(z)
	l_qz = dgm.gaussianLogDensity(z, z_m, z_lv)
	return tf.reduce_mean(l_px + l_py + l_pz - l_qz, axis=0)
	
    def qy_loss(self, x, y):
	""" compute the labeled penalty term of q(y|x) """
        y_ = dgm.forwardPassCatLogits(x, self.qy_x, self.n_hid, self.nonlinearity, self.bn, scope='qy_x')
        return dgm.multinoulliLogDensity(y, y_)

    def sample_z(self, x, y):
	""" get parameters of and samples from q(z|x,y) """
	qz_in = tf.concat([x, y], axis=-1)
        return dgm.samplePassGauss(qz_in, self.qz_xy, self.n_hid, self.nonlinearity, self.bn, mc_samps=self.mc_samples,  scope='qz_xy')

    def compute_logpx(self, x, z):
	""" compute the log density of x under p(x|z) """
	px_in = tf.reshape(z, [-1, self.n_z])
	if self.x_dist == 'Gaussian':
            mean, log_var = dgm.forwardPassGauss(px_in, self.px_z, self.n_hid, self.nonlinearity, self.bn, scope='px_z')
	    mean, log_var = tf.reshape(mean, [self.mc_samples, -1, self.n_x]),  tf.reshape(log_var, [self.mc_samples, -1, self.n_x])
            return dgm.gaussianLogDensity(x, mean, log_var)
        elif self.x_dist == 'Bernoulli':
            logits = dgm.forwardPassCatLogits(px_in, self.px_z, self.n_hid, self.nonlinearity, self.bn, scope='px_z')
	    logits = tf.reshape(logits, [self.mc_samples, -1, self.n_x])
            return dgm.bernoulliLogDensity(x, logits) 
  
    def compute_logpy(self, y, x, z):
	""" compute the log density of y under p(y|x,z)"""
	py_in = tf.reshape(tf.concat([x,z], axis=-1), [-1, self.n_x+self.n_z])
        y_ = dgm.forwardPassCatLogits(py_in, self.py_xz, self.n_hid, self.nonlinearity, self.bn, scope='py_xz')
	y_ = tf.reshape(y_, [self.mc_samples, -1, self.n_y])
        return dgm.multinoulliLogDensity(y, y_)

    def predict(self, x, n_iters=150):
	""" predict y for given x with p(y|x,z) """
	y_ = dgm.forwardPassCat(x, self.qy_x, self.n_hid, self.nonlinearity, self.bn, scope='qy_x')
        yq = y_
        y_ = tf.one_hot(tf.argmax(y_, axis=1), self.n_y)
        y_samps = tf.expand_dims(y_, axis=2)
        for i in range(n_iters):
            _, _, z = self.sample_z(x, y_)
	    z = tf.reshape(z, [-1, self.n_z])
            py_in = tf.concat([x, z], axis=-1)
            y_ = dgm.forwardPassCat(py_in, self.py_xz, self.n_hid, self.nonlinearity, self.bn, scope='py_xz')
            y_samps = tf.concat([y_samps, tf.expand_dims(y_, axis=2)], axis=2)
            y_ = tf.one_hot(tf.argmax(y_, axis=1), self.n_y)
        return tf.reduce_mean(y_samps, axis=2)

    def encode(self, x, y=None, n_iters=100):
	""" TODO: encode a new example into z-space (labeled or unlabeled) """
	if y is None:
	    y = tf.one_hot(tf.argmax(dgm.forwardPassCat(x, self.qy_x, self.n_hid, self.nonlinearity, self.bn, scope='qy_x'), axis=1), self.n_y)
	_, _, z = self.sample_z(x, y)
	return z

    def compute_acc(self, x, y):
	""" compute prediction accuracy for given input/outputs """
	y_ = self.predict(x)
	acc =  tf.reduce_mean(tf.cast(tf.equal(tf.argmax(y_,axis=1), tf.argmax(y, axis=1)), tf.float32))
	return acc 

    def initialize_networks(self):
    	""" Initialize all model networks """
	if self.x_dist == 'Gaussian':
      	    self.px_z = dgm.initGaussNet(self.n_z, self.n_hid, self.n_x, 'px_z_')
	elif self.x_dist == 'Bernoulli':
	    self.px_z = dgm.initCatNet(self.n_z, self.n_hid, self.n_x, 'px_z_')
    	self.qz_xy = dgm.initGaussNet(self.n_x+self.n_y, self.n_hid, self.n_z, 'qz_xy_')
    	self.qy_x = dgm.initCatNet(self.n_x, self.n_hid, self.n_y, 'qy_x_')
	self.py_xz = dgm.initCatNet(self.n_x+self.n_z, self.n_hid, self.n_y, 'py_xz_')

    def print_verbose1(self, epoch, fd, sess):
	total, elbo_l, elbo_u = sess.run([self.compute_loss(), self.elbo_l, self.elbo_u] ,fd)
	train_acc, test_acc = sess.run([self.train_acc, self.test_acc], fd)	
	print("Epoch: {}: Total: {:5.3f}, Labeled: {:5.3f}, Unlabeled: {:5.3f}, Training: {:5.3f}, Testing: {:5.3f}".format(epoch, total, elbo_l, elbo_u, train_acc, test_acc))	

    def print_verbose2(self, epoch, fd, sess):
	self.phase = False
	zm_test, zlv_test, z_test = self.sample_(self.x_test,self.y_test)
        zm_train, zlv_train, z_train = self.sample_z(self.x_train,self.y_train)
        lpx_test, lpx_train, acc_train, acc_test = sess.run([self.compute_logpx(self.x_test, z_test, self.y_test),
                                                                  self.compute_logpx(self.x_train, z_train, self.y_train),
                                                                  self.train_acc, self.test_acc], feed_dict=fd)
	print('Epoch: {}, logpx: {:5.3f}, Train: {:5.3f}, Test: {:5.3f}'.format(epoch, np.mean(lpx_train), np.mean(klz_train), acc_train, acc_test ))
