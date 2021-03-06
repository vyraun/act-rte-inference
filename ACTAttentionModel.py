
import tensorflow as tf
from tensorflow.python.ops import  rnn, rnn_cell, seq2seq
from embedding_utils import input_projection3D

class ACTAttentionModel(object):


    def __init__(self, config, pretrained_embeddings=None,
                 update_embeddings=True, is_training=False):

        self.config = config
        self.bidirectional = config.bidirectional
        self.batch_size = batch_size = config.batch_size
        self.hidden_size = hidden_size = config.hidden_size
        self.num_layers = 1
        self.vocab_size = config.vocab_size
        self.prem_steps = config.prem_steps
        self.hyp_steps = config.hyp_steps
        self.is_training = is_training
        # placeholders for inputs
        self.premise = tf.placeholder(tf.int32, [batch_size, self.prem_steps])
        self.hypothesis = tf.placeholder(tf.int32, [batch_size, self.hyp_steps])
        self.targets = tf.placeholder(tf.int32, [batch_size, 3])


        if pretrained_embeddings is not None:
            embedding = tf.get_variable('embedding', [self.vocab_size, self.config.embedding_size], dtype=tf.float32,
                                        trainable=update_embeddings)

            self.embedding_placeholder = tf.placeholder(tf.float32, [self.vocab_size, self.config.embedding_size])
            self.embedding_init = embedding.assign(self.embedding_placeholder)
        else:
            embedding = tf.get_variable('embedding', [self.vocab_size, self.hidden_size], dtype=tf.float32)

        # create lists of (batch,hidden_size) inputs for models
        premise_inputs = tf.nn.embedding_lookup(embedding, self.premise)
        hypothesis_inputs = tf.nn.embedding_lookup(embedding, self.hypothesis)

        if pretrained_embeddings is not None:
            with tf.variable_scope("input_projection"):
                premise_inputs = input_projection3D(premise_inputs, self.hidden_size)
            with tf.variable_scope("input_projection", reuse=True):
                hypothesis_inputs = input_projection3D(hypothesis_inputs, self.hidden_size)

        if self.config.no_cell:
            hyp_outputs = hypothesis_inputs
            premise_outputs = premise_inputs

        else:

            premise_inputs = [tf.squeeze(single_input, [1]) for single_input in tf.split(1, self.prem_steps, premise_inputs)]
            hypothesis_inputs = [tf.squeeze(single_input, [1]) for single_input in tf.split(1, self.hyp_steps, hypothesis_inputs)]

            with tf.variable_scope("premise_f"):
                prem_f = rnn_cell.GRUCell(self.config.encoder_size)
                self.prem_cell_f = rnn_cell.MultiRNNCell([prem_f]* self.num_layers)
            with tf.variable_scope("premise_b"):
                prem_b = rnn_cell.GRUCell(self.config.encoder_size)
                self.prem_cell_b = rnn_cell.MultiRNNCell([prem_b]* self.num_layers)

            # run GRUs over premise + hypothesis
            if self.bidirectional:
                premise_outputs, prem_state_f, prem_state_b = rnn.bidirectional_rnn(
                    self.prem_cell_f,self.prem_cell_b, premise_inputs,dtype=tf.float32, scope="gru_premise")
            else:
                premise_outputs, prem_state = rnn.rnn(
                    self.prem_cell_f, premise_inputs, dtype=tf.float32, scope="gru_premise")

            premise_outputs = tf.concat(1, [tf.expand_dims(x,1) for x in premise_outputs])

            with tf.variable_scope("hypothesis_f"):
                hyp_f = rnn_cell.GRUCell(self.config.encoder_size)
                self.hyp_cell_f = rnn_cell.MultiRNNCell([hyp_f] * self.num_layers)

            with tf.variable_scope("hypothesis_b"):
                hyp_b = rnn_cell.GRUCell(self.config.encoder_size)
                self.hyp_cell_b = rnn_cell.MultiRNNCell([hyp_b] * self.num_layers)

            if self.bidirectional:
                hyp_outputs, hyp_state_f, hyp_state_b = rnn.bidirectional_rnn(
                    self.hyp_cell_f,self.hyp_cell_b,hypothesis_inputs,dtype=tf.float32, scope= "gru_hypothesis")
            else:
                hyp_outputs, hyp_state = rnn.rnn(self.hyp_cell_f,hypothesis_inputs, dtype=tf.float32, scope="gru_hypothesis")

            hyp_outputs = tf.concat(1, [tf.expand_dims(x,1) for x in hyp_outputs])


        with tf.variable_scope("prediction"):
            prediction, stopping_probs, iterations = self.do_act_steps(
                                             premise_outputs, hyp_outputs)

        # make it easy to get this info out of the model later
        self.remainder = 1.0 - stopping_probs
        self.iterations = iterations
        #iterations = tf.Print(iterations, [iterations], message="Iterations: ", summarize=20)
        #remainder = tf.Print(remainder, [remainder], message="Remainder: ", summarize=20)
        # softmax over outputs to generate distribution over [neutral, entailment, contradiction]

        softmax_w = tf.get_variable("softmax_w", [2*self.rep_size, 3])
        softmax_b = tf.get_variable("softmax_b", [3])
        self.logits = tf.matmul(prediction, softmax_w) + softmax_b   # dim (batch_size, 3)

        _, targets = tf.nn.top_k(self.targets)

        loss = seq2seq.sequence_loss_by_example(
                [self.logits],
                [targets],
                [tf.ones([batch_size])],
                3)
        self.cost = tf.reduce_mean(loss) + self.config.step_penalty*tf.reduce_mean((self.remainder) + tf.cast(iterations, tf.float32))

        if self.config.embedding_reg and update_embeddings:
            self.cost += self.config.embedding_reg * (tf.reduce_mean(tf.square(embedding)))

        _, logit_max_index = tf.nn.top_k(self.logits)

        self.accuracy = tf.reduce_mean(tf.cast(tf.equal(logit_max_index, targets), tf.float32))

        if is_training:

            self.lr = tf.Variable(config.learning_rate, trainable=False)

            tvars = tf.trainable_variables()
            grads, _ = tf.clip_by_global_norm(tf.gradients(self.cost, tvars), self.config.max_grad_norm)

            #optimizer = tf.train.GradientDescentOptimizer(self.lr)
            optimizer = tf.train.AdamOptimizer(self.lr)
            self.train_op = optimizer.apply_gradients(zip(grads, tvars))

    def attention(self, query, attendees, scope):
        """Put attention masks on hidden using hidden_features and query."""

        attn_length = attendees.get_shape()[1].value
        attn_size = attendees.get_shape()[2].value

        with tf.variable_scope(scope):

            hidden = tf.reshape(attendees, [-1, attn_length, 1, attn_size])
            k = tf.get_variable("attention_W", [1,1,attn_size,attn_size])

            features = tf.nn.conv2d(hidden, k, [1, 1, 1, 1], "SAME")
            v = tf.get_variable("attention_v", [attn_size])

            with tf.variable_scope("attention"):

                y = tf.nn.rnn_cell._linear(query, attn_size, True)

                y = tf.reshape(y, [-1, 1, 1, attn_size])
                # Attention mask is a softmax of v^T * tanh(...).
                s = tf.reduce_sum(v * tf.tanh(features + y), [2, 3])
                a = tf.nn.softmax(s)
                # Now calculate the attention-weighted vector d.
                d = tf.reduce_sum(tf.reshape(a, [-1, attn_length, 1, 1]) * hidden,[1, 2])
                ds = tf.reshape(d, [-1, attn_size])

        return ds

    def do_act_steps(self, premise, hypothesis):


        self.rep_size = premise.get_shape()[-1].value

        self.one_minus_eps = tf.constant(1.0 - self.config.eps, tf.float32,[self.batch_size])
        self.N = tf.constant(self.config.max_computation, tf.float32,[self.batch_size])


        prob = tf.constant(0.0,tf.float32,[self.batch_size], name="prob")
        prob_compare = tf.constant(0.0,tf.float32,[self.batch_size], name="prob_compare")
        counter = tf.constant(0.0, tf.float32,[self.batch_size], name="counter")
        initial_state = tf.zeros([self.batch_size, 2*self.rep_size], tf.float32, name="state")
        acc_states = tf.zeros([self.batch_size,2*self.rep_size], tf.float32, name="state_accumulator")
        batch_mask = tf.constant(True, tf.bool,[self.batch_size])

        # While loop stops when this predicate is FALSE.
        # Ie all (probability < 1-eps AND counter < N) are false.

        pred = lambda batch_mask,prob_compare,prob,\
                      counter,state,premise, hypothesis ,acc_state:\
            tf.reduce_any(
                tf.logical_and(
                    tf.less(prob_compare,self.one_minus_eps),
                    tf.less(counter,self.N)))
                # only stop if all of the batch have passed either threshold

            # Do while loop iterations until predicate above is false.
        _,_,remainders,iterations,_,_,_,state = \
            tf.while_loop(pred,self.inference_step,
            [batch_mask,prob_compare,prob,
             counter,initial_state, premise, hypothesis, acc_states])

        return state, remainders, iterations

    def inference_step(self,batch_mask, prob_compare,prob,counter, state, premise, hypothesis, acc_states):

        if self.config.keep_prob < 1.0 and self.is_training:
            premise = tf.nn.dropout(premise, self.config.keep_prob)
            hypothesis = tf.nn.dropout(hypothesis,self.config.keep_prob)

        hyp_attn = self.attention(state, hypothesis, "hyp_attn")
        state_for_premise = tf.concat(1, [state, hyp_attn])
        prem_attn = self.attention(state_for_premise, premise, "prem_attn")
        new_state = tf.concat(1, [hyp_attn ,prem_attn])

        with tf.variable_scope('sigmoid_activation_for_pondering'):
            p = tf.squeeze(tf.sigmoid(tf.nn.rnn_cell._linear(new_state, 1, True)))


        new_batch_mask = tf.logical_and(tf.less(prob + p,self.one_minus_eps),batch_mask)
        new_float_mask = tf.cast(new_batch_mask, tf.float32)
        prob += p * new_float_mask
        prob_compare += p * tf.cast(batch_mask, tf.float32)

        def use_remainder():

            remainder = tf.constant(1.0, tf.float32,[self.batch_size]) - prob
            remainder_expanded = tf.expand_dims(remainder,1)
            tiled_remainder = tf.tile(remainder_expanded,[1,2*self.rep_size])

            acc_state = (new_state * tiled_remainder) + acc_states
            return acc_state

        def normal():

            p_expanded = tf.expand_dims(p * new_float_mask,1)
            tiled_p = tf.tile(p_expanded,[1,2*self.rep_size])

            acc_state = (new_state * tiled_p) + acc_states
            return acc_state


        counter += tf.constant(1.0,tf.float32,[self.batch_size]) * new_float_mask
        counter_condition = tf.less(counter,self.N)
        condition = tf.reduce_any(tf.logical_and(new_batch_mask,counter_condition))

        acc_state = tf.cond(condition, normal, use_remainder)

        return (new_batch_mask, prob_compare,prob,counter, new_state, premise, hypothesis, acc_state)

