# -*- coding: utf-8 -*-
"""SGNS Implementation.ipynb

Automatically generated by Colaboratory.

Original file is located at
    https://colab.research.google.com/drive/13NW6eURy37sNr4OrdjSeawsNdJuIv94l
"""

import torch
import torch.nn as nn

import numpy as np

import sys, time, os
from collections import Counter

!rm -rf wikipedia* *.zip*
!wget http://www.cse.chalmers.se/~richajo/dit865/slask/files/wikipedia_small.zip
!unzip wikipedia_small.zip
!ls

def make_ns_table(params):
    corpus = params['corpus']
    voc_size = params['voc-size']
    ns_table_size = params['ns-table-size']
    unk_str = params['unknown-str']
    lowercase = params['lowercase']
    ns_exp = params['ns-exp']

    # This is what we'll use to store the frequencies.
    freqs = Counter()

    print('Building vocabulary and sampling table...')    

    # First, build a full frequency table from the whole corpus.
    with open(corpus) as f:
        for i, line in enumerate(f, 1):
            if lowercase:
                line = line.lower()
            freqs.update(line.split())
            if i % 50000 == 0:
                sys.stdout.write('.')
                sys.stdout.flush()
            if i % 1000000 == 0:
                sys.stdout.write(' ')
                sys.stdout.write(str(i))
                sys.stdout.write('\n')
                sys.stdout.flush()
    print()

    # Sort the frequencies, then select the most frequent words as the vocabulary.
    freqs_sorted = sorted(freqs.items(),
                          key=lambda p: (p[1], p[0]),
                          reverse=True)
    if len(freqs_sorted) > voc_size-1:
        sum_freq_pruned = sum(f for _, f in freqs_sorted[voc_size-1:])
    else:
        sum_freq_pruned = 1

    # We'll add a special dummy to represent the occurrences of low-frequency words.
    freqs_sorted = [(unk_str, sum_freq_pruned)] + freqs_sorted[:voc_size-1]

    # Now, we'll compute the negative sampling table.
    # The negative sampling probabilities are proportional to the frequencies
    # to the power of a constant (typically 0.75).
    ns_table = {}
    sum_freq = 0
    for w, freq in freqs_sorted:
        ns_freq = freq ** ns_exp
        ns_table[w] = ns_freq
        sum_freq += ns_freq

    # Convert the negative sampling probabilities to integers, in order to make
    # sampling a bit faster and easier.
    # We return a list of tuples consisting of:
    # - the word
    # - its frequency in the training data
    # - the number of positions reserved for this word in the negative sampling table
    scaler = ns_table_size / sum_freq
    return [(w, freq, int(round(ns_table[w]*scaler))) for w, freq in freqs_sorted]

def load_ns_table(filename):
    with open(filename) as f:
        out = []
        for l in f:
            t = l.split()
            out.append((t[0], int(t[1]), int(t[2])))
        return out

def save_ns_table(table, filename):
    with open(filename, 'w') as f:
        for w, fr, ns in table:
            print(f'{w} {fr} {ns}', file=f)

class SGNSContextGenerator:

    def __init__(self, ns_table, params):

        # The name of the training file.
        self.corpus = params['corpus']
        
        # The string-to-integer mapping for the vocabulary.
        self.voc = { w:i for i, (w, _, _ ) in enumerate(ns_table) }

        # The number of positive instances we'll create in each batch.
        self.batch_size = params['batch-size']

        # The maximal width of the context window.
        self.ctx_width = params['context-width']

        # Whether we should 
        self.lowercase = params['lowercase']
        
        self.word_count = 0
        
        # We define the pruning probabilities for each word as in Mikolov's paper.
        total_freq = sum(f for _, f, _ in ns_table)
        self.prune_probs = {}
        for w, f, _ in ns_table:
            self.prune_probs[w] = 1 - np.sqrt(params['prune-threshold'] * total_freq / f)

    def prune(self, tokens):
        ps = np.random.random(size=len(tokens))
        # Remove some words from the input with probabilities defined by their frequencies.
        return [ w for w, p in zip(tokens, ps) if p >= self.prune_probs.get(w, 0) ]

    def batches(self):

        widths = np.random.randint(1, self.ctx_width+1, size=self.batch_size)
        width_ix = 0

        self.word_count = 0
        
        with open(self.corpus) as f:
            out_t = []
            out_c = []
            for line in f:

                # Process one line: lowercase and split into tokens.
                if self.lowercase:
                    line = line.lower()
                tokens = line.split()
                self.word_count += len(tokens)

                # Remove some words, then encode as integers.
                encoded = [ self.voc.get(t, 0) for t in self.prune(tokens) ]

                for i, t in enumerate(encoded):

                    # The context width is selected uniformly between 1 and the maximal width.
                    w = widths[width_ix]
                    width_ix += 1

                    # Compute start and end positions for the context.
                    start = max(0, i-w)
                    end = min(i+w+1, len(encoded))

                    # Finally, generate target--context pairs.
                    for j in range(start, end):
                        if j != i:
                            out_t.append(encoded[i])
                            out_c.append(encoded[j])
                            
                            # If we've generate enough pairs, yield a batch.
                            # Each batch is a list of targets and a list of corresponding contexts.
                            if len(out_t) == self.batch_size:
                                yield out_t, out_c
                                
                                # After coming back, reset the batch.
                                widths = np.random.randint(1, self.ctx_width+1, size=self.batch_size)
                                width_ix = 0
                                out_t = []
                                out_c = []
                    
            print('End of file.')
            if len(out_t) > 0:
                # Yield the final batch.
                yield out_t, out_c

# Next, we implement the neural network that defines the model. 
# The parameters just consist of two sets of embeddings: one for the target words, and one for the contexts.

# The forward step is fairly trivial: we just compute the dot products of the target and context embeddings.
# As usual, the most annoying part is to keep track of the tensor shapes.

# We also add a couple of methods that allow us to inspect the model: computing the cosine similarity between the embeddings for two words,
# and finding the nearest neighbor lists of a set of words.


class SGNSModel(nn.Module):

    def __init__(self, voc, params):
        super().__init__()
        
        voc_size = len(voc)
        
        # Target word embeddings
        self.w = nn.Embedding(voc_size, params['emb-dim'])
        # Context embeddings
        self.c = nn.Embedding(voc_size, params['emb-dim'])
        
        # Some things we need to print nearest neighbor lists for diagnostics.
        self.voc = voc
        self.ivoc = { i:w for w, i in voc.items() }

    def forward(self, tgt, ctx):       
        # tgt is a 1-dimensional tensor containing target word ids
        # ctx is a 2-dimensional tensor containing positive and negative context ids for each target
        
        # Look up the embeddings for the target words.
        # shape: (batch size, embedding dimension)
        tgt_emb = self.w(tgt)
        
        n_batch, emb_dim = tgt_emb.shape
        n_ctx = ctx.shape[1]
        
        # View this as a 3-dimensional tensor, with
        # shape (batch size, 1, embedding dimension)
        tgt_emb = tgt_emb.view(n_batch, 1, emb_dim)

        # Look up the embeddings for the positive and negative context words.
        # shape: (batch size, nbr contexts, emb dim)
        ctx_emb = self.c(ctx)

        # Transpose the tensor for matrix multiplication
        # shape: (batch size, emb dim, nbr contexts)
        ctx_emb = ctx_emb.transpose(1, 2)

        # Compute the dot products between target word embeddings and context
        # embeddings. We express this as a batch matrix multiplication (bmm).
        # shape: (batch size, 1, nbr contexts)
        dots = tgt_emb.bmm(ctx_emb)

        # View this result as a 2-dimensional tensor.
        # shape: (batch size, nbr contexts)
        dots = dots.view(n_batch, n_ctx)

        return dots
    
    
    def nearest_neighbors(self, words, n_neighbors):
        
        # Encode the words as integers, and put them into a PyTorch tensor.
        words_ix = torch.as_tensor([self.voc[w] for w in words])
        
        # Look up the embeddings for the test words.
        voc_size, emb_dim = self.w.weight.shape
        test_emb = self.w(words_ix).view(len(words), 1, emb_dim)

        # Also, get the embeddings for all words in the vocabulary.
        all_emb = self.w.weight.view(1, voc_size, emb_dim)

        # We'll use a cosine similarity function to find the most similar words.
        # The .view kludgery above is needed for the batch-wise cosine similarity.
        sim_func = nn.CosineSimilarity(dim=2)
        scores = sim_func(test_emb, all_emb)
        # The shape of scores is (nbr of test words, total number of words)
                
        # Find the top-scoring columns in each row.
        if not n_neighbors:
            n_neighbors = self.n_testwords_neighbors
        near_nbr = scores.topk(n_neighbors+1, dim=1)
        values = near_nbr.values[:,1:]
        indices = near_nbr.indices[:, 1:]
        
        # Finally, map word indices back to strings, and put the result in a list.
        out = []
        for ixs, vals in zip(indices, values):
            out.append([ (self.ivoc[ix.item()], val.item()) for ix, val in zip(ixs, vals) ])
        return out
        
        
    def cosine_similarity(self, word1, word2):        
        # We just look up the two embeddings and use PyTorch's built-in cosine similarity.
        v1 = self.w(torch.as_tensor(self.voc[word1]))
        v2 = self.w(torch.as_tensor(self.voc[word2]))
        sim = nn.CosineSimilarity(dim=0)
        return sim(v1, v2).item()

# NEXT STEP IS TRAINING
# The following calss contains the training loop: it creates  a batch of positive target-context pairs, generates negative samples,
# and then updates the embedding model.

class SGNSTrainer:

    def __init__(self, instance_gen, model, ns_table, params):
        self.instance_gen = instance_gen
        self.model = model
        self.n_epochs = params['n-epochs']
        self.max_words = params.get('max-words')
        n_batch = params['batch-size']
        self.n_ns = params['n-neg-samples']

        if params['optimizer'] == 'adam':
            self.optimizer = torch.optim.Adam(self.model.parameters(), lr=params['lr'])
        elif params['optimizer'] == 'sgd':
            self.optimizer = torch.optim.SGD(self.model.parameters(), lr=params['lr'])

        # We'll use a binary cross-entropy loss, since we have a binary classification problem:
        # distinguishing positive from negative contexts.
        self.loss = nn.BCEWithLogitsLoss()

        # Build the negative sampling table.
        ns_table_expanded = []
        for i, (_, _, count) in enumerate(ns_table):
            ns_table_expanded.extend([i] * count)
        self.ns_table = torch.as_tensor(ns_table_expanded)
        
        # Define the "gold standard" that we'll use to compute the loss.
        # It consists of a column of ones, and then a number of columns of zeros.
        # This structure corresponds to the positive and negative contexts, respectively.
        y_pos = torch.ones((n_batch, 1))
        y_neg = torch.zeros((n_batch, self.n_ns))
        self.y = torch.cat([y_pos, y_neg], dim=1)

        # Some things we need to print nearest neighbor lists for diagnostics.
        #self.voc = instance_gen.voc
        #self.ivoc = { i:w for w, i in self.voc.items() }
        self.testwords = params['testwords']
        self.n_testwords_neighbors = params['n-testwords-neighbors']

        self.epoch = 0
        
    def print_test_nearest_neighbors(self):
                
        nn_lists = self.model.nearest_neighbors(self.testwords, self.n_testwords_neighbors)
        
        # For each test word, print the most similar words.
        for w, nn_list in zip(self.testwords, nn_lists):
            print(w, end=':\n')
            for nn, sim in nn_list:
                print(f' {nn} ({sim:.3f})', end='')
            print()
        
        print('------------------------------------')
        
    def make_negative_sample(self, batch_size):
        neg_sample_ixs = torch.randint(len(self.ns_table), (batch_size, self.n_ns))
        return self.ns_table.take(neg_sample_ixs)
            
    def train(self):

        print_interval = 5000000
        
        while self.epoch < self.n_epochs:
            print(f'Epoch {self.epoch+1}.')

            # For diagnostics.
            n_pairs = 0
            sum_loss = 0
            total_pairs = 0
            n_batches = 0
            t0 = time.time()
            
            for t, c_pos in self.instance_gen.batches():

                batch_size = len(t)
                
                # Put the encoded target words and contexts into PyTorch tensors.
                t = torch.as_tensor(t)                
                c_pos = torch.as_tensor(c_pos)
                c_pos = c_pos.view(batch_size, 1)
                
                # Generate a sample of fake context words.
                # shape: (batch size, number of negative samples)
                c_neg = self.make_negative_sample(batch_size)
                
                # Combine positive and negative contexts.
                # shape: (batch size, 1 + nbr neg samples)
                c = torch.cat([c_pos, c_neg], dim=1)
                
                self.optimizer.zero_grad()

                # Compute the output from the model.
                # That is, the dot products between target embeddings
                # and context embeddings.
                scores = self.model(t, c)

                # Compute the loss with respect to the gold standard.
                loss = self.loss(scores, self.y[:batch_size])

                # Compute gradients and update the embeddings.
                loss.backward()
                self.optimizer.step()

                # We'll print some diagnostics periodically.
                sum_loss += loss.item()
                n_pairs += batch_size
                n_batches += 1
                if n_pairs > print_interval:
                    total_words = self.instance_gen.word_count
                    total_pairs += n_pairs
                    t1 = time.time()                    
                    print(f'Pairs: {total_pairs}, words: {total_words}, loss: {sum_loss / n_batches:.4f}, time: {t1-t0:.2f}')
                    self.print_test_nearest_neighbors()
                    n_pairs = 0
                    sum_loss = 0
                    n_batches = 0
                    t0 = time.time()
                    
            self.epoch += 1

# Putting pieces together

model = None

def main():
    global model
    params = {
        'corpus': 'wikipedia_small/wikipedia.txt', # Training data file
        'device': 'cuda', # Device

        'n-neg-samples': 5, # Number of negative samples per positive sample
        'emb-dim': 64, # Embedding dimensionality
        
        'n-epochs': 2, # Number of epochs
        
        'batch-size': 1<<20, # Number of positive training instances in one batch
        'context-width': 5, # Maximal possible context width
        'prune-threshold': 1e-3, # Pruning threshold (see Mikolov's paper)
        'voc-size': 100000, # Maximal vocabulary size
        'ns-table-file': 'ns_table.txt', # Where to store the negative sampling table
        'ns-table-size': 1<<24, # Size of negative sampling table
        'ns-exp': 0.75, # Smoothing parameter for negative sampling distribution (see paper)
        'unknown-str': '<UNKNOWN>', # Dummy token for low-frequency words
        'lowercase': True, # Whether to lowercase the text
        'optimizer': 'adam', # Which gradient descent optimizer to use
        'lr': 1e-1, # Learning rate for the  optimizer

        # The test words for which we print the nearest neighbors periodically
        'testwords': ['apple', 'terrible', 'sweden', '1979', 'write', 'gothenburg'],
        # Number of nearest neighbors
        'n-testwords-neighbors': 5,
    }
    
    if params['device'] == 'cuda' and torch.cuda.is_available():
        torch.set_default_tensor_type(torch.cuda.FloatTensor)
        print('Running on CUDA device.')
    else:
        torch.set_default_tensor_type(torch.FloatTensor)
        print('Running on CPU.')

    # If we didn't already create the vocabulary and negative 
    # sampling table, we'll do that now.
    if os.path.exists(params['ns-table-file']):
        ns_table = load_ns_table(params['ns-table-file'])
    else:
        ns_table = make_ns_table(params)
        save_ns_table(ns_table, params['ns-table-file'])

    ctx_gen = SGNSContextGenerator(ns_table, params)
    model = SGNSModel(ctx_gen.voc, params)
    trainer = SGNSTrainer(ctx_gen, model, ns_table, params)

    trainer.train()
        
main()

# Model Inspecting

model.nearest_neighbors(['potato'], 5)

model.cosine_similarity('monkey', 'lion')

model.cosine_similarity('dog', 'dog')

