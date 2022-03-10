# -*- coding: utf-8 -*-
"""Deep Anomaly Detection on Attributed Networks (Dominant)"""
# Author: Kay Liu <zliu234@uic.edu>
# License: BSD 2 clause

import torch
from math import ceil
import torch.nn as nn
import torch.nn.functional as F
from torch_sparse import SparseTensor
from torch_geometric.nn import GCN
from sklearn.utils.validation import check_is_fitted

from . import BaseDetector
from ..evaluator.metric import roc_auc_score


class Dominant(BaseDetector):
    """
    Dominant(Deep Anomaly Detection on Attributed Networks)
    Dominant is an anomaly detector consisting of a shared graph
    convolutional encoder, a structure reconstruction decoder, and an
    attribute reconstruction decoder. The reconstruction mean square
    error of the decoders are defined as structure anomaly score and
    attribute anomaly score, respectively.

    See :cite:`ding2019deep` for details.

    Parameters
    ----------
    hid_dim :  int, optional
        Hidden dimension of model. Defaults: ``0``.
    num_layers : int, optional
        Total number of layers in model. A half (ceil) of the layers
        are for the encoder, the other half (floor) of the layers are
        for decoders. Defaults: ``4``.
    dropout : float, optional
        Dropout rate. Defaults: ``0.``.
    weight_decay : float, optional
        Weight decay (L2 penalty). Defaults: ``0.``.
    act : callable activation function or None, optional
        Activation function if not None.
        Defaults: ``torch.nn.functional.relu``.
    alpha : float, optional
        loss balance weight for attribute and structure.
        Defaults: ``0.5``.
    contamination : float, optional
        Valid in (0., 0.5). The proportion of outliers in the data set.
        Used when fitting to define the threshold on the decision
        function. Defaults: ``0.1``.
    lr : float, optional
        Learning rate. Defaults: ``0.004``.
    epoch : int, optional
        Maximum number of training epoch. Defaults: ``100``.
    gpu : int
        GPU Index, -1 for using CPU. Defaults: ``0``.
    verbose : bool
        Verbosity mode. Turn on to print out log information.
        Defaults: ``False``.

    Examples
    --------
    >>> from pygod.models import Dominant
    >>> model = Dominant()
    >>> model.fit(data)
    >>> prediction = model.predict(data)
    """
    def __init__(self,
                 hid_dim=64,
                 num_layers=4,
                 dropout=0.3,
                 weight_decay=0.,
                 act=F.relu,
                 alpha=0.8,
                 contamination=0.1,
                 lr=5e-3,
                 epoch=100,
                 gpu=0,
                 verbose=False):
        super(Dominant, self).__init__(contamination=contamination)

        # model param
        self.hid_dim = hid_dim
        self.num_layers = num_layers
        self.dropout = dropout
        self.weight_decay = weight_decay
        self.act = act
        self.alpha = alpha

        # training param
        self.lr = lr
        self.epoch = epoch
        if gpu >= 0 and torch.cuda.is_available():
            self.device = 'cuda:{}'.format(gpu)
        else:
            self.device = 'cpu'

        # other param
        self.verbose = verbose
        self.model = None

    def fit(self, G):
        """
        Description
        -----------
        Fit detector with input data.

        Parameters
        ----------
        G : PyTorch Geometric Data instance (torch_geometric.data.Data)
            The input data.

        Returns
        -------
        self : object
            Fitted estimator.
        """

        x, adj, edge_index, labels = self.process_graph(G)

        self.model = Dominant_Base(in_dim=x.shape[1],
                                   hid_dim=self.hid_dim,
                                   num_layers=self.num_layers,
                                   dropout=self.dropout,
                                   act=self.act).to(self.device)

        optimizer = torch.optim.Adam(self.model.parameters(),
                                     lr=self.lr,
                                     weight_decay=self.weight_decay)

        for epoch in range(self.epoch):
            self.model.train()
            x_, adj_ = self.model(x, edge_index)
            score = self.loss_func(x, x_, adj, adj_)
            loss = torch.mean(score)

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

            if self.verbose:
                # TODO: support more metrics
                auc = roc_auc_score(labels, score.detach().cpu().numpy())
                print("Epoch {:04d}: Loss {:.4f} | AUC {:.4f}"
                      .format(epoch, loss.item(), auc))

        self.decision_scores_ = score.detach().cpu().numpy()
        self._process_decision_scores()
        return self

    def decision_function(self, G):
        """
        Description
        -----------
        Predict raw anomaly score using the fitted detector. Outliers
        are assigned with larger anomaly scores.

        Parameters
        ----------
        G : PyTorch Geometric Data instance (torch_geometric.data.Data)
            The input data.

        Returns
        -------
        outlier_scores : numpy.ndarray
            The anomaly score of shape :math:`N`.
        """
        check_is_fitted(self, ['model'])

        # get needed data object from the input data
        x, adj, edge_index, _ = self.process_graph(G)

        # enable the evaluation mode
        self.model.eval()

        # construct the vector for holding the reconstruction error
        x_, adj_ = self.model(x, edge_index)
        outlier_scores = self.loss_func(x, x_, adj, adj_)
        return outlier_scores.detach().cpu().numpy()

    def process_graph(self, G):
        """
        Description
        -----------
        Process the raw PyG data object into a tuple of sub data
        objects needed for the model.

        Parameters
        ----------
        G : PyTorch Geometric Data instance (torch_geometric.data.Data)
            The input data.

        Returns
        -------
        x : torch.Tensor
            Attribute (feature) of nodes.
        adj : torch.Tensor
            Adjacency matrix of the graph.
        edge_index : torch.Tensor
            Edge list of the graph.
        y : torch.Tensor
            Labels of nodes.
        """
        edge_index = G.edge_index
        
        # TODO: potential memory efficient improvement
        #  via sparse matrix operation
        dense_adj \
            = SparseTensor(row=edge_index[0], col=edge_index[1]).to_dense()

        # adjacency matrix normalization
        rowsum = dense_adj.sum(1)
        d_inv_sqrt = torch.pow(rowsum, -0.5).flatten()
        d_inv_sqrt[torch.isinf(d_inv_sqrt)] = 0.
        d_mat_inv_sqrt = torch.diag(d_inv_sqrt)
        adj = (dense_adj * d_mat_inv_sqrt).T * d_mat_inv_sqrt

        edge_index = edge_index.to(self.device)
        adj = adj.to(self.device)
        x = G.x.to(self.device)
        y = G.y

        # return data objects needed for the network
        return x, adj, edge_index, y

    def loss_func(self, x, x_, adj, adj_):
        # attribute reconstruction loss
        diff_attribute = torch.pow(x - x_, 2)
        attribute_errors = torch.sqrt(torch.sum(diff_attribute, 1))

        # structure reconstruction loss
        diff_structure = torch.pow(adj - adj_, 2)
        structure_errors = torch.sqrt(torch.sum(diff_structure, 1))

        score = self.alpha * attribute_errors \
                + (1 - self.alpha) * structure_errors
        return score


class Dominant_Base(nn.Module):
    def __init__(self,
                 in_dim,
                 hid_dim,
                 num_layers,
                 dropout,
                 act):

        super(Dominant_Base, self).__init__()

        # split the number of layers for the encoder and decoders
        encoder_layers = ceil(num_layers / 2)
        decoder_layers = num_layers - encoder_layers

        self.shared_encoder = GCN(in_channels=in_dim,
                                  hidden_channels=hid_dim,
                                  num_layers=encoder_layers,
                                  out_channels=hid_dim,
                                  dropout=dropout,
                                  act=act)

        self.attr_decoder = GCN(in_channels=hid_dim,
                                hidden_channels=hid_dim,
                                num_layers=decoder_layers,
                                out_channels=in_dim,
                                dropout=dropout,
                                act=act)

        self.struct_decoder = GCN(in_channels=hid_dim,
                                  hidden_channels=hid_dim,
                                  num_layers=decoder_layers - 1,
                                  out_channels=in_dim,
                                  dropout=dropout,
                                  act=act)

    def forward(self, x, edge_index):
        # encode
        h = self.shared_encoder(x, edge_index)
        # decode feature matrix
        x_ = self.attr_decoder(h, edge_index)
        # decode adjacency matrix
        h_ = self.struct_decoder(h, edge_index)
        adj_ = h_ @ h_.T

        # return reconstructed matrices
        return x_, adj_
