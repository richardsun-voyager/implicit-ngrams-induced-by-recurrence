import numpy as np
import torch 
from torch import nn
from torch.nn import functional as F
from matplotlib import pyplot as plt
from torch.nn import utils as nn_utils
import torch
import pickle
import numpy as np
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torch.autograd import Variable
import torch.nn.init as init
stdv = 0.1
def length_to_mask(length, max_len=None, dtype=None):
    """length: B.
    return B x max_len.
    If max_len is None, then max of length will be used.
    """
    assert len(length.shape) == 1, 'Length shape should be 1 dimensional.'
    max_len = max_len or length.max().item()
    mask = torch.arange(max_len, device=length.device,
                        dtype=length.dtype).expand(len(length), max_len) < length.unsqueeze(1)
    if dtype is not None:
        mask = torch.as_tensor(mask, dtype=dtype, device=length.device)
    return mask

def adjust_order(batch_size, max_len, lengths):
  '''
  adjust order for the elements for the RNN in the backward direction, make the padding tokens in the beginning.
  e.g., [[1,2,3], [2,3,0]] -> [[1,2,3], [0, 2, 3]]
  '''
  assert batch_size == len(lengths)
  index = torch.arange(0, max_len).type_as(lengths).expand(batch_size, max_len)
  shift = lengths.expand(max_len, batch_size).transpose(0, 1)
  new_index = (index + shift)%max_len
  return new_index


bound = 1.0


class RNNCell(nn.Module):
  def __init__(self, input_size, hidden_size, bias=False, num_chunks=1):
    super(RNNCell, self).__init__()
    self.input_size = input_size
    self.hidden_size = hidden_size
    self.bias = bias
    self.weight_ih = nn.Parameter(torch.Tensor(num_chunks*hidden_size, input_size))
    self.weight_hh = nn.Parameter(torch.Tensor(num_chunks*hidden_size, hidden_size))
    self.weight_bias = nn.Parameter(torch.Tensor(1, num_chunks * hidden_size))
    self.num_chunks = num_chunks
    self.reset_parameters()
    self.hardtanh = nn.Hardtanh(-bound, bound)
    print('MVMA-E')

  def reset_parameters(self):
    '''
    This is important to curb the range of the initializations.
    '''
    stdv = 1.0 / np.sqrt(self.hidden_size)
    for weight in self.parameters():
        init.uniform_(weight, -stdv, stdv)
  
  def init_hidden(self, batch_size):
    weight = next(self.parameters())
    return weight.new_zeros(batch_size, self.hidden_size)

  def forward(self, x, hidden):
    '''
    x: batch_size, input_size
    h: batch_size, hidden_size
    '''
    gi = F.linear(x, self.weight_ih)
    if self.bias:
        gi = gi + self.weight_bias
    gh = F.linear(hidden, self.weight_hh)

# ##******************************linear gru*********************
    g = torch.tanh(gi)
    f1 = 1 - g**2
#     f2 = -g*f1
    hidden = g + f1*gh#+f2*gh**2############original
    newgate, inputgate, resetgate = 0, 0, 0
    return hidden, newgate, inputgate, resetgate

class RNN(nn.Module):
    def __init__(self, input_size, hidden_size, bidirectional=False):
      super(RNN, self).__init__()
      self.f_cell = RNNCell(input_size, hidden_size)
      if bidirectional:
        self.b_cell = RNNCell(input_size, hidden_size)
      self.bidirectional = bidirectional
      self.input_size = input_size
      self.hidden_size = hidden_size

    def forward(self, x, lengths=None, hidden=None):
      '''
      x: batch_size*max_len*emb_dim
      length: batch_size
      '''
      batch_size, max_len, emb_dim = x.size()
      #Create masks
      masks = length_to_mask(lengths, max_len, dtype=torch.float)
      #Initialize the hidden state
      if not hidden:
        f_prev_hidden = self.f_cell.init_hidden(batch_size)
      f_seq = []
      for i in range(max_len):
        cur_input = x[:, i, :]
        mask = masks[:, i].expand(self.hidden_size, batch_size).transpose(0, 1)
        f_hidden, _, _, _ = self.f_cell(cur_input, f_prev_hidden)
        f_hidden = f_hidden * mask+f_prev_hidden * (1 - mask)#mask the padding values
        f_seq.append(f_hidden)
        f_prev_hidden = f_hidden

      f_seq = torch.stack(f_seq, dim=1)
      #Backward direction
      if self.bidirectional:
        #Consider the padding case
        permutate_index = adjust_order(batch_size, max_len, lengths)
        #put the padding tokens in front
        permutate_mask = torch.gather(masks, 1, permutate_index)
        permutate_index_rep = permutate_index.expand(emb_dim, 
                                                     batch_size, max_len).transpose(0, 1).transpose(1, 2)
        permutate_x = torch.gather(x, 1, permutate_index_rep)
        b_seq = []
        if not hidden:
          b_prev_hidden = self.f_cell.init_hidden(batch_size)
        for i in reversed(range(max_len)):
          cur_input = permutate_x[:, i, :]
          cur_mask = permutate_mask[:, i].expand(self.hidden_size, 
                                                       batch_size)
          cur_mask = cur_mask.transpose(0, 1)

          b_hidden, _, _, _ = self.b_cell(cur_input, b_prev_hidden)
          #The values of the padding positions stay still
          b_hidden = b_hidden * cur_mask + b_prev_hidden * ( 1 - cur_mask)
          b_seq.append(b_hidden)
          b_prev_hidden = b_hidden
        #b_seq = list(reversed(b_seq))
        b_seq = torch.stack(b_seq, dim=1)
        #restore the order
        permutate_index_rep = permutate_index.expand(self.hidden_size, 
                                                     batch_size, max_len).transpose(0, 1).transpose(1, 2)
        b_seq = torch.gather(b_seq, 1, permutate_index_rep)
        b_seq = torch.flip(b_seq, (1, ))
        seq = torch.cat([f_seq, b_seq], dim=2)
        cat_hidden = torch.cat([f_hidden, b_hidden], 1)
        return seq, cat_hidden
      return f_seq, f_hidden