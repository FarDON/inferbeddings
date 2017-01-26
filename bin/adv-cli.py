#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import logging
import math
import sys
import os

import numpy as np
import tensorflow as tf

from inferbeddings.io import read_triples
from inferbeddings.knowledgebase import Fact, KnowledgeBaseParser

from inferbeddings.parse import parse_clause

from inferbeddings.models import base as models
from inferbeddings.models import similarities

from inferbeddings.models.training import losses, pairwise_losses, constraints, corrupt, index
from inferbeddings.models.training.util import make_batches

from inferbeddings.adversarial import Adversarial, GroundLoss

from inferbeddings import evaluation

logger = logging.getLogger(os.path.basename(sys.argv[0]))


def train(session, train_sequences, nb_entities, nb_predicates, nb_batches, seed, similarity_name, entity_embedding_size, predicate_embedding_size, hidden_size,
          model_name, loss_name, pairwise_loss_name, margin, learning_rate, nb_epochs, parser,
          clauses, adv_lr, adversary_epochs, discriminator_epochs, adv_weight, adv_margin,
          adv_batch_size, adv_init_ground, adv_ground_samples, adv_ground_tol, predicate_l2, predicate_norm, debug):

    index_gen = index.GlorotIndexGenerator()
    neg_idxs = np.arange(nb_entities)

    subject_corruptor = corrupt.SimpleCorruptor(index_generator=index_gen, candidate_indices=neg_idxs, corrupt_objects=False)
    object_corruptor = corrupt.SimpleCorruptor(index_generator=index_gen, candidate_indices=neg_idxs, corrupt_objects=True)

    # Saving training examples in two Numpy matrices, Xr (nb_samples, 1) containing predicate ids,
    # and Xe (nb_samples, 2), containing subject and object ids.
    Xr = np.array([[rel_idx] for (rel_idx, _) in train_sequences])
    Xe = np.array([ent_idxs for (_, ent_idxs) in train_sequences])

    nb_samples = Xr.shape[0]

    # Number of samples per batch.
    batch_size = math.ceil(nb_samples / nb_batches)

    # Input for the subject and object ids.
    entity_inputs = tf.placeholder(tf.int32, shape=[None, 2])

    # Input for the predicate id - at the moment it is a length-1 walk, i.e. a predicate id only,
    # but it can correspond to a sequence of predicates (a walk in the knowledge graph).
    walk_inputs = tf.placeholder(tf.int32, shape=[None, None])

    np.random.seed(seed)
    random_state = np.random.RandomState(seed)
    tf.set_random_seed(seed)

    # Instantiate the model
    similarity_function = similarities.get_function(similarity_name)

    entity_embedding_layer = tf.get_variable('entities', shape=[nb_entities + 1, entity_embedding_size],
                                             initializer=tf.contrib.layers.xavier_initializer())

    predicate_embedding_layer = tf.get_variable('predicates', shape=[nb_predicates + 1, predicate_embedding_size],
                                                initializer=tf.contrib.layers.xavier_initializer())

    entity_embeddings = tf.nn.embedding_lookup(entity_embedding_layer, entity_inputs)
    predicate_embeddings = tf.nn.embedding_lookup(predicate_embedding_layer, walk_inputs)

    model_class = models.get_function(model_name)

    model_parameters = dict(entity_embeddings=entity_embeddings,
                            predicate_embeddings=predicate_embeddings,
                            similarity_function=similarity_function,
                            entity_embedding_size=entity_embedding_size,
                            predicate_embedding_size=predicate_embedding_size,
                            hidden_size=hidden_size)
    model = model_class(**model_parameters)

    # Scoring function used for scoring arbitrary triples.
    score = model()

    def scoring_function(args):
        return session.run(score, feed_dict={walk_inputs: args[0], entity_inputs: args[1]})

    loss_function = 0.0

    adversarial, ground_loss, clause_to_feed_dicts = None, None, None
    if adv_lr is not None:
        adversarial = Adversarial(clauses=clauses, parser=parser, predicate_embedding_layer=predicate_embedding_layer,
                                  model_class=model_class, model_parameters=model_parameters, loss_margin=adv_margin,
                                  batch_size=adv_batch_size)

        if adv_ground_samples is not None:
            ground_loss = GroundLoss(clauses=clauses, parser=parser, scoring_function=scoring_function, tolerance=adv_ground_tol)

            # For each clause, sample a list of 1024 {variable: entity} mappings
            entity_indices = sorted({idx for idx in parser.entity_to_index.values()})
            clause_to_feed_dicts = {clause: GroundLoss.sample_mappings(GroundLoss.get_variable_names(clause),
                                                                       entities=entity_indices,
                                                                       sample_size=adv_ground_samples) for clause in clauses}

        initialize_violators = tf.variables_initializer(var_list=adversarial.parameters, name='init_violators')
        violation_errors, violation_loss = adversarial.errors, adversarial.loss

        adv_opt_scope_name = 'adversarial/optimizer'
        with tf.variable_scope(adv_opt_scope_name):
            violation_finding_optimizer = tf.train.AdagradOptimizer(learning_rate=adv_lr)
            violation_training_step = violation_finding_optimizer.minimize(- violation_loss, var_list=adversarial.parameters)

        adversarial_optimizer_variables = tf.get_collection(tf.GraphKeys.GLOBAL_VARIABLES, scope=adv_opt_scope_name)
        adversarial_optimizer_variables_initializer = tf.variables_initializer(adversarial_optimizer_variables)

        loss_function += adv_weight * violation_loss
        adversarial_projection_steps = [constraints.renorm_update(adversarial_embedding_layer, norm=1.0)
                                        for adversarial_embedding_layer in adversarial.parameters]

    # Loss function to minimize by means of Stochastic Gradient Descent.
    fact_loss = 0.0
    if loss_name is not None:
        loss = losses.get_function(loss_name)
        target = tf.cast((tf.range(0, limit=tf.shape(score)[0]) % 2) < 1, score.dtype)
        fact_loss += loss(score, target)
    else:
        # Transform the pairwise loss function in an unary loss function,
        # where each positive example is followed by a negative example.
        def pairwise_to_unary_modifier(_loss_function):
            def unary_function(scores, *_args, **_kwargs):
                positive_scores, negative_scores = tf.split(1, 2, tf.reshape(scores, [-1, 2]))
                return _loss_function(positive_scores, negative_scores, *_args, **_kwargs)
            return unary_function

        pairwise_loss = pairwise_to_unary_modifier(pairwise_losses.get_function(pairwise_loss_name))
        fact_loss += pairwise_loss(score, margin=margin)

    if predicate_l2 is not None:
        fact_loss += tf.nn.l2_loss(predicate_embedding_layer)

    loss_function += fact_loss

    # Optimization algorithm being used.
    optimizer = tf.train.AdagradOptimizer(learning_rate=learning_rate)
    trainable_var_list = [entity_embedding_layer, predicate_embedding_layer] + model.get_params()
    training_step = optimizer.minimize(loss_function, var_list=trainable_var_list)

    # We enforce all entity embeddings to have an unitary norm.
    projection_steps = [constraints.renorm_update(entity_embedding_layer, norm=1.0)]
    if predicate_norm is not None:
        projection_steps += [constraints.renorm_update(predicate_embedding_layer, norm=predicate_norm)]

    init_op = tf.global_variables_initializer()

    prev_embedding_matrix = None
    initialize_violators, adversarial_optimizer_variables_initializer = None, None

    session.run(init_op)

    for epoch in range(1, nb_epochs + 1):

        if clause_to_feed_dicts is not None:
            sum_errors = 0
            for clause_idx, clause in enumerate(clauses):
                feed_dicts = clause_to_feed_dicts[clause]
                nb_errors = ground_loss.zero_one_errors(clause=clause, feed_dicts=feed_dicts)
                logger.info('Epoch: {}\tClause index: {}\tZero-One Errors: {}'.format(epoch, clause_idx, nb_errors))
                sum_errors += nb_errors
            logger.info('Epoch: {}\tSum of Zero-One Errors: {}'.format(epoch, sum_errors))

        for disc_epoch in range(1, discriminator_epochs + 1):
            order = random_state.permutation(nb_samples)
            Xr_shuf, Xe_shuf = Xr[order, :], Xe[order, :]

            Xr_sc, Xe_sc = subject_corruptor(Xr_shuf, Xe_shuf)
            Xr_oc, Xe_oc = object_corruptor(Xr_shuf, Xe_shuf)

            batches = make_batches(nb_samples, batch_size)
            loss_values, violation_loss_values = [], []
            total_fact_loss_value = 0

            for batch_start, batch_end in batches:
                curr_batch_size = batch_end - batch_start

                Xr_batch = np.zeros((curr_batch_size * 4, Xr.shape[1]), dtype=Xr.dtype)
                Xe_batch = np.zeros((curr_batch_size * 4, Xe.shape[1]), dtype=Xe.dtype)

                Xr_batch[0::4, :] = Xr_batch[2::4, :] = Xr[batch_start:batch_end, :]
                Xe_batch[0::4, :] = Xe_batch[2::4, :] = Xe[batch_start:batch_end, :]

                Xr_batch[1::4, :], Xe_batch[1::4, :] = Xr_sc[batch_start:batch_end, :], Xe_sc[batch_start:batch_end, :]
                Xr_batch[3::4, :], Xe_batch[3::4, :] = Xr_oc[batch_start:batch_end, :], Xe_oc[batch_start:batch_end, :]

                loss_args = {walk_inputs: Xr_batch, entity_inputs: Xe_batch}

                if adv_lr is not None:
                    _, loss_value, fact_loss_value, violation_loss_value = session.run([training_step, loss_function, fact_loss, violation_loss],
                                                                                       feed_dict=loss_args)
                    violation_loss_values += [violation_loss_value]
                else:
                    _, loss_value, fact_loss_value = session.run([training_step, loss_function, fact_loss],
                                                                 feed_dict=loss_args)

                loss_values += [loss_value / Xr_batch.shape[0]]
                total_fact_loss_value += fact_loss_value

                for projection_step in projection_steps:
                    session.run([projection_step])

            def stats(values):
                return '{0:.4f} ± {1:.4f}'.format(round(np.mean(values), 4), round(np.std(values), 4))

            logger.info('Epoch: {0}/{1}\tLoss: {2}'.format(epoch, disc_epoch, stats(loss_values)))
            logger.info('Epoch: {0}/{1}\tFact Loss: {2:.4f}'.format(epoch, disc_epoch, total_fact_loss_value))

            if adv_lr is not None:
                logger.info('Epoch: {0}/{1}\tViolation Loss: {2}'.format(epoch, disc_epoch, stats(violation_loss_values)))

        if adv_lr is not None:
            logger.info('Finding violators ..')

            session.run([initialize_violators, adversarial_optimizer_variables_initializer])

            if adv_init_ground:
                # Initialize the violating embeddings using real embeddings
                def ground_init_op(violating_embeddings):
                    # Select adv_batch_size random entity indices - first collect all entity indices
                    _ent_indices = np.array(sorted(parser.index_to_entity.keys()))
                    # Then select a subset of size adv_batch_size of such indices
                    rnd_ent_indices = _ent_indices[random_state.randint(low=0, high=len(_ent_indices), size=adv_batch_size)]
                    # Assign the embeddings of the entities at such indices to the violating embeddings
                    _ent_embeddings = tf.nn.embedding_lookup(entity_embedding_layer, rnd_ent_indices)
                    return violating_embeddings.assign(_ent_embeddings)

                assignment_ops = [ground_init_op(violating_emb) for violating_emb in adversarial.parameters]
                session.run(assignment_ops)

            for projection_step in adversarial_projection_steps:
                session.run([projection_step])

            for finding_epoch in range(1, adversary_epochs + 1):
                _, violation_errors_value, violation_loss_value = session.run([violation_training_step, violation_errors, violation_loss])

                if finding_epoch == 1 or finding_epoch % 10 == 0:
                    logger.info('Epoch: {}, Finding Epoch: {}, Violated Clauses: {}, Violation loss: {}'
                                .format(epoch, finding_epoch, int(violation_errors_value), round(violation_loss_value, 4)))

                for projection_step in adversarial_projection_steps:
                    session.run([projection_step])

        if debug:
            from inferbeddings.visualization import hinton_diagram
            embedding_matrix = session.run(predicate_embedding_layer)[1:, :]
            print(hinton_diagram(embedding_matrix))

            if prev_embedding_matrix is not None:
                diff = prev_embedding_matrix - embedding_matrix
                logger.info('Epoch: {}, Update to Predicate Embeddings: {} Norm: {}'.format(epoch, np.abs(diff).sum(), np.abs(embedding_matrix).sum()))

            prev_embedding_matrix = embedding_matrix

    objects = {
        'entity_embedding_layer': entity_embedding_layer,
        'predicate_embedding_layer': predicate_embedding_layer
    }

    return scoring_function, objects


def main(argv):
    def formatter(prog):
        return argparse.HelpFormatter(prog, max_help_position=100, width=200)

    argparser = argparse.ArgumentParser('Rule Injection via Adversarial Training', formatter_class=formatter)

    argparser.add_argument('--train', '-t', required=True, action='store', type=str, default=None)
    argparser.add_argument('--valid', '-v', action='store', type=str, default=None)
    argparser.add_argument('--test', '-T', action='store', type=str, default=None)

    argparser.add_argument('--debug', '-D', action='store_true', help='Debug flag')

    argparser.add_argument('--lr', '-l', action='store', type=float, default=0.1)

    argparser.add_argument('--nb-batches', '-b', action='store', type=int, default=10)
    argparser.add_argument('--nb-epochs', '-e', action='store', type=int, default=100)

    argparser.add_argument('--model', '-m', action='store', type=str, default='DistMult', help='Model')
    argparser.add_argument('--similarity', '-s', action='store', type=str, default='dot', help='Similarity function')

    argparser.add_argument('--loss', action='store', type=str, default=None, help='Loss function')
    argparser.add_argument('--pairwise-loss', action='store', type=str, default='hinge_loss',
                           help='Pairwise loss function')

    argparser.add_argument('--margin', '-M', action='store', type=float, default=1.0, help='Margin')

    argparser.add_argument('--embedding-size', '--entity-embedding-size', '-k', action='store', type=int, default=10,
                           help='Entity embedding size')
    argparser.add_argument('--predicate-embedding-size', '-p', action='store', type=int, default=None,
                           help='Predicate embedding size')
    argparser.add_argument('--hidden-size', '-H', action='store', type=int, default=None,
                           help='Size of the hidden layer (if necessary, e.g. ER-MLP)')

    argparser.add_argument('--predicate-l2', action='store', type=float, default=None,
                           help='Weight of the L2 regularization term on the predicate embeddings')
    argparser.add_argument('--predicate-norm', action='store', type=float, default=None,
                           help='Norm of the predicate embeddings')

    argparser.add_argument('--auc', '-a', action='store_true',
                           help='Measure the predictive accuracy using AUC-PR and AUC-ROC')
    argparser.add_argument('--seed', '-S', action='store', type=int, default=0, help='Seed for the PRNG')

    argparser.add_argument('--clauses', '-c', action='store', type=str, default=None,
                           help='File containing background knowledge expressed as Horn clauses')

    argparser.add_argument('--adv-lr', '-L', action='store', type=float, default=None, help='Adversary learning rate')

    argparser.add_argument('--adversary-epochs', action='store', type=int, default=10,
                           help='Adversary - number of training epochs')
    argparser.add_argument('--discriminator-epochs', action='store', type=int, default=1,
                           help='Discriminator - number of training epochs')

    argparser.add_argument('--adv-weight', '-W', action='store', type=float, default=1.0, help='Adversary weight')
    argparser.add_argument('--adv-margin', action='store', type=float, default=0.0, help='Adversary margin')

    argparser.add_argument('--adv-batch-size', action='store', type=int, default=1,
                           help='Size of the batch of adversarial examples to use')
    argparser.add_argument('--adv-init-ground', action='store_true',
                           help='Initialize adversarial embeddings using real entity embeddings')

    argparser.add_argument('--adv-ground-samples', action='store', type=int, default=None,
                           help='Number of ground samples on which to compute the ground loss')
    argparser.add_argument('--adv-ground-tol', '--adv-ground-tolerance', action='store', type=float, default=0.0,
                           help='Epsilon-tolerance when calculating the ground loss')

    argparser.add_argument('--save', action='store', type=str, default=None, help='Path for saving the serialized model')

    args = argparser.parse_args(argv)

    train_path, valid_path, test_path = args.train, args.valid, args.test
    nb_batches, nb_epochs = args.nb_batches, args.nb_epochs
    learning_rate, margin = args.lr, args.margin

    model_name, similarity_name = args.model, args.similarity
    loss_name, pairwise_loss_name = args.loss, args.pairwise_loss
    entity_embedding_size, predicate_embedding_size = args.embedding_size, args.predicate_embedding_size
    hidden_size = args.hidden_size

    predicate_l2, predicate_norm = args.predicate_l2, args.predicate_norm

    if predicate_embedding_size is None:
        predicate_embedding_size = entity_embedding_size

    is_auc = args.auc
    seed = args.seed
    debug = args.debug

    clauses_path = args.clauses

    adv_lr, adv_weight, adv_margin = args.adv_lr, args.adv_weight, args.adv_margin
    adversary_epochs, discriminator_epochs = args.adversary_epochs, args.discriminator_epochs
    adv_ground_samples, adv_ground_tol = args.adv_ground_samples, args.adv_ground_tol
    adv_batch_size, adv_init_ground = args.adv_batch_size, args.adv_init_ground

    save_path = args.save

    assert train_path is not None
    pos_train_triples, _ = read_triples(train_path)
    pos_valid_triples, neg_valid_triples = read_triples(valid_path) if valid_path else (None, None)
    pos_test_triples, neg_test_triples = read_triples(test_path) if test_path else (None, None)

    def fact(s, p, o):
        return Fact(predicate_name=p, argument_names=[s, o])

    train_facts = [fact(s, p, o) for s, p, o in pos_train_triples]

    valid_facts = [fact(s, p, o) for s, p, o in pos_valid_triples] if pos_valid_triples is not None else []
    valid_facts_neg = [fact(s, p, o) for s, p, o in neg_valid_triples] if neg_valid_triples is not None else []

    test_facts = [fact(s, p, o) for s, p, o in pos_test_triples] if pos_test_triples is not None else []
    test_facts_neg = [fact(s, p, o) for s, p, o in neg_test_triples] if neg_test_triples is not None else []

    logger.info('#Training Triples: {}, #Validation Triples: {}, #Test Triples: {}'.format(len(train_facts), len(valid_facts), len(test_facts)))

    parser = KnowledgeBaseParser(train_facts + valid_facts + test_facts)

    nb_entities = len(parser.entity_vocabulary)
    nb_predicates = len(parser.predicate_vocabulary)

    logger.info('#Entities: {}\t#Predicates: {}'.format(nb_entities, nb_predicates))

    train_sequences = parser.facts_to_sequences(train_facts)

    valid_sequences = parser.facts_to_sequences(valid_facts)
    valid_sequences_neg = parser.facts_to_sequences(valid_facts_neg)

    test_sequences = parser.facts_to_sequences(test_facts)
    test_sequences_neg = parser.facts_to_sequences(test_facts_neg)

    # Parse the clauses
    clauses = None

    if adv_lr is not None:
        assert clauses_path is not None

    if clauses_path is not None:
        with open(clauses_path, 'r') as f:
            clauses = [parse_clause(line.strip()) for line in f.readlines()]

    # Do not take up all the GPU memory, all the time.
    sess_config = tf.ConfigProto()
    sess_config.gpu_options.allow_growth = True

    with tf.Session(config=sess_config) as session:
        scoring_function, objects = train(session, train_sequences, nb_entities, nb_predicates, nb_batches, seed, similarity_name,
                                          entity_embedding_size, predicate_embedding_size, hidden_size,
                                          model_name, loss_name, pairwise_loss_name, margin, learning_rate, nb_epochs, parser,
                                          clauses, adv_lr, adversary_epochs, discriminator_epochs, adv_weight, adv_margin,
                                          adv_batch_size, adv_init_ground, adv_ground_samples, adv_ground_tol,
                                          predicate_l2, predicate_norm, debug)

        if save_path is not None:
            import pickle

            objects_to_serialize = {
                'command_line': argv,
                'entity_to_index': parser.entity_to_index,
                'predicate_to_index': parser.predicate_to_index,
                'entities': objects['entity_embedding_layer'].eval(),
                'predicates': objects['predicate_embedding_layer'].eval()
            }

            with open('{}.pkl'.format(save_path), 'wb') as f:
                pickle.dump(objects_to_serialize, f)
            logger.info('Model parameters saved in {}.pkl'.format(save_path))

            saver = tf.train.Saver()
            save_path = saver.save(session, '{}.model.ckpt'.format(save_path))
            logger.info('Model saved in {}'.format(save_path))

        train_triples = [(s, p, o) for (p, [s, o]) in train_sequences]

        valid_triples = [(s, p, o) for (p, [s, o]) in valid_sequences]
        valid_triples_neg = [(s, p, o) for (p, [s, o]) in valid_sequences_neg]

        test_triples = [(s, p, o) for (p, [s, o]) in test_sequences]
        test_triples_neg = [(s, p, o) for (p, [s, o]) in test_sequences_neg]

        true_triples = train_triples + valid_triples + test_triples

        if valid_triples:
            if is_auc:
                evaluation.evaluate_auc(scoring_function, valid_triples, valid_triples_neg, nb_entities, nb_predicates, tag='valid')
            else:
                evaluation.evaluate_ranks(scoring_function, valid_triples, nb_entities, true_triples=true_triples, tag='valid')

        if test_triples:
            if is_auc:
                evaluation.evaluate_auc(scoring_function, test_triples, test_triples_neg, nb_entities, nb_predicates, tag='test')
            else:
                evaluation.evaluate_ranks(scoring_function, test_triples, nb_entities, true_triples=true_triples, tag='test')

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    main(sys.argv[1:])
