import torch
from torch import nn
from torch.nn import functional as F

from ss_opm.model.torch_function import correl_loss
from ss_opm.utility.metadata_utility import CELL_TYPES
from ss_opm.model.encoder_decoder.sim_siam_module import SimSiamModule, PredictionModule
from ss_opm.utility.targets_values_normalize import targets_values_normalize_torch
from ss_opm.model.torch_dataset.citeseq_dataset import METADATA_KEYS


class CiteEncoderDecoderModule(nn.Module):

    def __init__(
            self,
            x_dim,
            y_dim,
            y_statistic,
            encoder_h_dim,
            decoder_h_dim,
            n_decoder_block,
            inputs_decomposer_components,
            targets_decomposer_components,
            encoder,
            decoder,
            simsiam=False,
    ):
        super(CiteEncoderDecoderModule, self).__init__()
        self.x_dim = x_dim
        self.y_dim = y_dim
        self.info_dim = len(METADATA_KEYS)
        self.encoder = encoder
        self.decoder = decoder
        self.y_loc = torch.nn.Parameter(y_statistic["y_loc"], requires_grad=False)
        self.y_scale = torch.nn.Parameter(y_statistic["y_scale"], requires_grad=False)
        self.inputs_decomposer_components = torch.nn.Parameter(inputs_decomposer_components, requires_grad=False)
        self.targets_decomposer_components = torch.nn.Parameter(targets_decomposer_components, requires_grad=False)
        self.targets_global_median = torch.nn.Parameter(y_statistic["targets_global_median"], requires_grad=False)
        self.correl_loss_func = correl_loss
        #self.mse_loss_func = nn.MSELoss()
        self.mae_loss_func = nn.L1Loss()
        self.gender_embedding = torch.nn.Parameter(torch.rand(2, encoder_h_dim))
        self.encoder_in_fc = nn.Linear(x_dim + self.info_dim, encoder_h_dim)
        decoder_out_fcs = []
        for i in range(n_decoder_block + 1):
            decoder_out_fcs.append(nn.Linear(decoder_h_dim, y_dim))
        self.decoder_out_fcs = nn.ModuleList(decoder_out_fcs)

    def _encode(self, x, gender_id, info):
        h = torch.hstack((x, info.reshape((x.shape[0], self.info_dim))))
        h = self.encoder_in_fc(h)
        h = h + self.gender_embedding[gender_id]
        z, _ = self.encoder(h)
        return z

    def _decode(self, z, cell_type_id_pred, mask_prob):
        h = z
        _, hs = self.decoder(h)
        ys = []
        for i, h in enumerate(hs):
            new_h = hs[i]
            y_base = self.decoder_out_fcs[i](new_h)
            y = y_base * self.y_scale[None, :] + self.y_loc[None, :]
            ys.append(y)
        return ys

    def forward(self, x, gender_id, nonzero_ratio):
        z = self._encode(x, gender_id, nonzero_ratio)
        y_preds = self._decode(z, None, None)
        return y_preds

    def loss(self, x,  gender_id, info, y, preprocessed_y, x_us, gender_id_us, info_us, training_length_ratio):
        loss = 0
        loss_corr = 0
        loss_mse = 0
        loss_res = 0.0
        z = self._encode(x=x, gender_id=gender_id, info=info)
        y_preds = self._decode(z, None, None)

        for i in range(len(y_preds)):
            y_pred = y_preds[i]
            postprocessed_y_pred = torch.matmul(y_pred, self.targets_decomposer_components) + self.targets_global_median[None, :]
            loss_corr = loss_corr + self.correl_loss_func(postprocessed_y_pred, y)
            loss_mse = loss_mse + self.mae_loss_func(y_pred, preprocessed_y)
        w = (1 - training_length_ratio)**2
        loss_corr /= len(y_preds)
        loss = loss + loss_corr
        loss_mse /= len(y_preds)
        loss = loss + w*loss_mse
        return loss, loss_corr, loss_mse, 0.0, 0.0

    def predict(self, x, gender_id, info):
        y_preds = self(x, gender_id, info)
        postprocessed_y_pred = None
        for i in range(len(y_preds)):
            new_postprocessed_y_pred = targets_values_normalize_torch(torch.matmul(y_preds[i], self.targets_decomposer_components) + self.targets_global_median[None, :])
            if postprocessed_y_pred is None:
                postprocessed_y_pred = new_postprocessed_y_pred
            else:
                postprocessed_y_pred += new_postprocessed_y_pred
        postprocessed_y_pred /= len(y_preds)
        return postprocessed_y_pred