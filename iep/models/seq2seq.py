#!/usr/bin/env python3

# Copyright 2017-present, Facebook, Inc.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

import torch
import torch.cuda
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable

from iep.embedding import expand_embedding_vocab


class Seq2Seq(nn.Module):
    def __init__(self,
                 encoder_vocab_size=100,
                 decoder_vocab_size=100,
                 wordvec_dim=300,
                 hidden_dim=256,
                 rnn_num_layers=2,
                 rnn_dropout=0,
                 null_token=0,
                 start_token=1,
                 end_token=2,
                 encoder_embed=None
                 ):
        super(Seq2Seq, self).__init__()
        self.encoder_embed = nn.Embedding(encoder_vocab_size, wordvec_dim)
        self.encoder_rnn = nn.LSTM(wordvec_dim, hidden_dim, rnn_num_layers,
                                   dropout=rnn_dropout, batch_first=True)
        self.decoder_embed = nn.Embedding(decoder_vocab_size, wordvec_dim)
        self.decoder_rnn = nn.LSTM(
            wordvec_dim + hidden_dim,
            hidden_dim,
            rnn_num_layers,
            dropout=rnn_dropout,
            batch_first=True)
        self.decoder_linear = nn.Linear(hidden_dim, decoder_vocab_size)
        self.NULL = null_token
        self.START = start_token
        self.END = end_token
        self.multinomial_outputs = None

    def expand_encoder_vocab(self, token_to_idx, word2vec=None, std=0.01):
        expand_embedding_vocab(self.encoder_embed, token_to_idx,
                               word2vec=word2vec, std=std)

    def get_dims(self, x=None, y=None):
        V_in = self.encoder_embed.num_embeddings
        V_out = self.decoder_embed.num_embeddings
        D = self.encoder_embed.embedding_dim
        H = self.encoder_rnn.hidden_size
        L = self.encoder_rnn.num_layers

        N = x.size(0) if x is not None else None
        N = y.size(0) if N is None and y is not None else N
        T_in = x.size(1) if x is not None else None
        # if y is not None:
        #  #print(y.size(), " in get_dim")
        T_out = y.size(1) if y is not None else None
        return V_in, V_out, D, H, L, N, T_in, T_out

    def before_rnn(self, x, replace=0):
        # TODO: Use PackedSequence instead of manually plucking out the last
        # non-NULL entry of each sequence; it is cleaner and more efficient.
        N, T = x.size()
        idx = torch.LongTensor(N).fill_(T - 1)
        #print("Idx np size :", idx.size())

        # Find the last non-null element in each sequence. Is there a clean
        # way to do this?
        x_cpu = x.cpu()
        #print(" data x : ", x.size())
        #print("tensor x : ", x.type())
        for i in range(N):
            for t in range(T - 1):
                if x_cpu.data[i,
                              t] != self.NULL and x_cpu.data[i,
                                                             t + 1] == self.NULL:
                    idx[i] = t
                    break
        idx = idx.type_as(x.data)
        #print("x.data :", x.data[0])
        #print("Idx np tensor size :", idx.size())
        #print("x_cpu and x : ", x_cpu, x)
        x[x.data == self.NULL] = replace
        #print("after x : ", x)
        #print("idx : ", idx)
        return x, Variable(idx)

    def encoder(self, x, check_ac=False):
        #print("Shape of X : ", x.size())
        V_in, V_out, D, H, L, N, T_in, T_out = self.get_dims(x=x)
        x, idx = self.before_rnn(x)
        #if check_ac:
        #    print("x and idx are : ", x, " ", idx)
        #print("Shape of X after before_rnn:", x.size())
        embed = self.encoder_embed(x)
        #print("embed : ", embed)
        #print("Embed ka shape :", embed.size())
        h0 = Variable(torch.zeros(L, N, H).type_as(embed.data))
        c0 = Variable(torch.zeros(L, N, H).type_as(embed.data))
        #print("h0 c0 ka shape : ", h0.size(), " and ", c0.size())
        #print("embed : ", embed)
        out, _ = self.encoder_rnn(embed, (h0, c0))
        #print("out : ", out)

        # Pull out the hidden state for the last non-null value in each input
        #print(" Idx shape : ", idx.size(), " out shape : ", out.size())
        idx = idx.view(N, 1, 1).expand(N, 1, H)
        #print("idx value ", idx)
        out = out.gather(1, idx)
        #print("t : ", out)
        #print("out shape after gather : ", out.view(N, H))
        return out.view(N, H)
        # return out.gather(1, idx).view(N, H)

    def decoder(self, encoded, y, h0=None, c0=None):
        V_in, V_out, D, H, L, N, T_in, T_out = self.get_dims(y=y)

        if T_out > 1:
            y, _ = self.before_rnn(y)
        #print("y ka shape after before_rnn : ", y.size())
        y_embed = self.decoder_embed(y)
        #print("y_embed ka shape after embedding : ", y_embed.size())
        y_embed = y_embed.view(y_embed.size()[0], -1, y_embed.size()[-1])
        encoded_repeat = encoded.view(N, 1, H).expand(N, T_out, H)
        #print("Encoded Repeat ka shape : ", encoded_repeat.size(), " y_embed ka shape : ", y_embed.size())
        rnn_input = torch.cat([encoded_repeat, y_embed], 2)
        if h0 is None:
            h0 = Variable(torch.zeros(L, N, H).type_as(encoded.data))
        if c0 is None:
            c0 = Variable(torch.zeros(L, N, H).type_as(encoded.data))
        rnn_output, (ht, ct) = self.decoder_rnn(rnn_input, (h0, c0))

        rnn_output_2d = rnn_output.contiguous().view(N * T_out, H)
        output_logprobs = self.decoder_linear(
            rnn_output_2d).view(N, T_out, V_out)

        return output_logprobs, ht, ct

    def compute_loss(self, output_logprobs, y):
        """
    Compute loss. We assume that the first element of the output sequence y is
    a start token, and that each element of y is left-aligned and right-padded
    with self.NULL out to T_out. We want the output_logprobs to predict the
    sequence y, shifted by one timestep so that y[0] is fed to the network and
    then y[1] is predicted. We also don't want to compute loss for padded
    timesteps.

    Inputs:
    - output_logprobs: Variable of shape (N, T_out, V_out)
    - y: LongTensor Variable of shape (N, T_out)
    """
        self.multinomial_outputs = None
        V_in, V_out, D, H, L, N, T_in, T_out = self.get_dims(y=y)
        print(V_in, V_out, D, H, L, N, T_in, T_out)
        #print("y.data : ", y)
        mask = y.data != self.NULL
        #print("--- Y before masking  ka shape :", y.size())
        #print("--- mask ka shape : ", mask.size())
        y_mask = Variable(torch.Tensor(N, T_out).fill_(0).type_as(mask))
        #print("--- y_mask ka shape : ", y_mask.size())
        y_mask[:, 1:] = mask[:, 1:]
        y_masked = y[y_mask]
        #print("--- Y_masked ka shape :", y_masked.size())
        out_mask = Variable(torch.Tensor(N, T_out).fill_(0).type_as(mask))
        #print("--- out_mask ka shape : ", out_mask.size())
        #print("--- mask ka shape : ", mask.size())
        out_mask[:, :-1] = mask[:, 1:]
        out_mask = out_mask.view(N, T_out, 1).expand(N, T_out, V_out)
        #print("--- out_mask ka shape after broadcast : ", out_mask.size())
        out_masked = output_logprobs[out_mask].view(-1, V_out)
        #print("--- out_masked ka shape :", out_masked.size())
        loss = F.cross_entropy(out_masked, y_masked)
        return loss

    def forward(self, x, y):
        encoded = self.encoder(x)
        #print("encoded shape : ", encoded.size())
        output_logprobs, _, _ = self.decoder(encoded, y)
        #print("output logprobs size : ", output_logprobs.size())
        loss = self.compute_loss(output_logprobs, y)
        #print("loss : ", loss)
        return loss

    def sample(self, x, max_length=50):
        # TODO: Handle sampling for minibatch inputs
        # TODO: Beam search?
        self.multinomial_outputs = None
        #print("x : ", x)
        assert x.size(0) == 1, "Sampling minibatches not implemented"
        encoded = self.encoder(x, True)
        #print("encoded :", encoded)
        y = [self.START]
        h0, c0 ,i = None, None, 0
        while True:
            cur_y = Variable(torch.LongTensor(
                [y[-1]]).type_as(x.data).view(1, 1))
            if i==0 : print("cur_y ", cur_y)
            logprobs, h0, c0 = self.decoder(encoded, cur_y, h0=h0, c0=c0)
            if i==0 : print("logprobs : ", logprobs)
            _, next_y = logprobs.data.max(2)
            print("next y shape and value : ", next_y.size(), next_y)
            y.append(next_y[0, 0, 0])
            i = 1
            if len(y) >= max_length or y[-1] == self.END:
                break
        return y

    def reinforce_sample(
            self,
            x,
            max_length=30,
            temperature=1.0,
            argmax=False):
        N, T = x.size(0), max_length
        #print("N, T  : ", N, T)
        encoded = self.encoder(x)
        #print("encoded shape : ", encoded.size())
        y = torch.LongTensor(N, T).fill_(self.NULL)
        done = torch.ByteTensor(N).fill_(0)
        #print("y shape : ", y.size()," done shape : ", done.size())
        cur_input = Variable(x.data.new(N, 1).fill_(self.START))
        #print("cur_iput shape : ", cur_input.size())
        h, c = None, None
        self.multinomial_outputs = []
        self.multinomial_probs = []
        for t in range(T):
            # logprobs is N x 1 x V
            logprobs, h, c = self.decoder(encoded, cur_input, h0=h, c0=c)
            #print("logprobs shape and type: ", logprobs.size(), type(logprobs))
            logprobs = logprobs / temperature
            probs = F.softmax(logprobs.view(N, -1))  # Now N x V
            #print("probs shape and type: ", probs.size(), type(probs))
            if argmax:
                _, cur_output = probs.max(1)
            else:
                cur_output = probs.multinomial()  # Now N x 1
            self.multinomial_outputs.append(cur_output)
            self.multinomial_probs.append(probs)
            cur_output_data = cur_output.data.cpu()
            not_done = logical_not(done)
            y[:, t][not_done] = cur_output_data[not_done]
            done = logical_or(done, cur_output_data.cpu() == self.END)
            cur_input = cur_output
            if done.sum() == N:
                break
        return Variable(y.type_as(x.data))

    def reinforce_backward(self, reward, output_mask=None):
        """
    If output_mask is not None, then it should be a FloatTensor of shape (N, T)
    giving a multiplier to the output.
    """
        assert self.multinomial_outputs is not None, 'Must call reinforce_sample first'
        grad_output = []
        print("in reinforce backward --- ")
        def gen_hook(mask):
            def hook(grad):
                print("grad value : ", grad.size(), grad.data)
                return grad * mask.contiguous().view(-1, 1).expand_as(grad)

            return hook

        if output_mask is not None:
            for t, probs in enumerate(self.multinomial_probs):
                mask = Variable(output_mask[:, t])
                probs.register_hook(gen_hook(mask))
        #print("value of output_mask - ", output_mask)
        print("multi op size : ", len(self.multinomial_outputs), self.multinomial_outputs[0].size())
        for sampled_output in self.multinomial_outputs:
            #print("sampled output : ", sampled_output)
            sampled_output.reinforce(reward)
            #print("sampled output after reinforce: ", sampled_output)
            grad_output.append(None)
        print(self.multinomial_outputs)
        print("--------------")
        print(grad_output)
        torch.autograd.backward(
            self.multinomial_outputs,
            grad_output,
            retain_variables=True)


def logical_and(x, y):
    return x * y


def logical_or(x, y):
    return (x + y).clamp_(0, 1)


def logical_not(x):
    return x == 0
