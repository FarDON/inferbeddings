# -*- coding: utf-8 -*-

import json

import numpy as np
import tensorflow as tf

from tensorflow.contrib import rnn
from tensorflow.contrib import legacy_seq2seq as S

from inferbeddings.nli import tfutil


class IScorer:
    def __init__(self, embedding_layer, token_to_index):
        self.embedding_layer = embedding_layer
        self.token_to_index = token_to_index
        self.vocab_size = max(self.token_to_index.values()) + 1




class LMScorer:
    def __init__(self, embedding_layer, token_to_index, lm_path='models/lm/', batch_size=32):
        self.embedding_layer = embedding_layer
        self.token_to_index = token_to_index
        self.vocab_size = max(self.token_to_index.values()) + 1

        with open('{}/config.json'.format(lm_path), 'r') as f:
            lm_config = json.load(f)

        self.lm_seq_length = 1
        self.lm_batch_size = batch_size
        self.lm_rnn_size = lm_config['rnn_size']
        self.lm_num_layers = lm_config['num_layers']

        lm_cell_fn = rnn.BasicLSTMCell
        lm_cells = [lm_cell_fn(self.lm_rnn_size) for _ in range(self.lm_num_layers)]
        self.lm_cell = rnn.MultiRNNCell(lm_cells)

        self.lm_scope_name = 'language_model'
        with tf.variable_scope(self.lm_scope_name):
            self.lm_input_data_ph = tf.placeholder(tf.int32, [None, self.lm_seq_length], name='input_data')
            self.lm_targets_ph = tf.placeholder(tf.int32, [None, self.lm_seq_length], name='targets')
            self.lm_initial_state = self.lm_cell.zero_state(self.lm_batch_size, tf.float32)

            with tf.variable_scope('rnnlm'):
                lm_W = tf.get_variable(name='W', shape=[self.lm_rnn_size, self.vocab_size],
                                       initializer=tf.contrib.layers.xavier_initializer())
                lm_b = tf.get_variable(name='b', shape=[self.vocab_size], initializer=tf.zeros_initializer())

                lm_emb_lookup = tf.nn.embedding_lookup(embedding_layer, self.lm_input_data_ph)
                lm_emb_projection = tf.contrib.layers.fully_connected(inputs=lm_emb_lookup, num_outputs=self.lm_rnn_size,
                                                                      weights_initializer=tf.contrib.layers.xavier_initializer(),
                                                                      biases_initializer=tf.zeros_initializer())

                lm_inputs = tf.split(lm_emb_projection, self.lm_seq_length, 1)
                lm_inputs = [tf.squeeze(input_, [1]) for input_ in lm_inputs]

            lm_outputs, lm_last_state = S.rnn_decoder(decoder_inputs=lm_inputs, initial_state=self.lm_initial_state,
                                                      cell=self.lm_cell, loop_function=None, scope='rnnlm')
            lm_output = tf.reshape(tf.concat(lm_outputs, 1), [-1, self.lm_rnn_size])

            lm_logits = tf.matmul(lm_output, lm_W) + lm_b
            self.lm_loss = S.sequence_loss_by_example(logits=[lm_logits], targets=[tf.reshape(self.lm_targets_ph, [-1])],
                                                      weights=[tf.ones([self.lm_batch_size * self.lm_seq_length])])
            self.lm_cost = tf.reduce_sum(self.lm_loss) / self.lm_batch_size / self.lm_seq_length
            self.lm_final_state = lm_last_state

    def get_lm_vars(self):
        lm_vars = tfutil.get_variables_in_scope(self.lm_scope_name)
        return lm_vars

    def log_perplexity(self, session, sentences, sizes):
        assert sentences.shape[0] == sizes.shape[0]
        _batch_size = sentences.shape[0]

        x = np.zeros(shape=(_batch_size, 1))
        y = np.zeros(shape=(_batch_size, 1))

        _sentences, _sizes = sentences[:, 1:], sizes[:] - 1
        state = session.run(self.lm_cell.zero_state(_batch_size, tf.float32))
        loss_values = []

        for j in range(_sizes.max() - 1):
            x[:, 0] = _sentences[:, j]
            y[:, 0] = _sentences[:, j + 1]

            feed = {
                self.lm_input_data_ph: x, self.lm_targets_ph: y, self.lm_initial_state: state
            }
            loss_value, state = session.run([self.lm_loss, self.lm_final_state], feed_dict=feed)
            loss_values += [loss_value]

        loss_values = np.array(loss_values).transpose()
        __sizes = _sizes - 2

        res = np.array([np.sum(loss_values[_i, :__sizes[_i]]) for _i in range(loss_values.shape[0])])
        return res
