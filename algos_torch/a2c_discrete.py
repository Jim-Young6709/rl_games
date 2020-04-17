import common.a2c_common
from torch import optim
import torch 
from torch import nn
import algos_torch.torch_ext
import numpy as np

class DiscreteA2CAgent(common.a2c_common.DiscreteA2CBase):
    def __init__(self, base_name, observation_space, action_space, config):
        common.a2c_common.DiscreteA2CBase.__init__(self, base_name, observation_space, action_space, config)
        
        config = {
            'actions_num' : self.actions_num,
            'input_shape' : self.state_shape,
            'games_num' : 1,
            'batch_num' : 1,
        } 
        self.model = self.network.build(config)
        self.model.cuda()
        self.last_lr = float(self.last_lr)
        self.optimizer = optim.Adam(self.model.parameters(), float(self.last_lr))
    def update_epoch(self):
        self.epoch_num += 1
        return self.epoch_num

    def _preproc_obs(self, obs_batch):
        if obs_batch.dtype == np.uint8:
            obs_batch = torch.cuda.ByteTensor(obs_batch)
            obs_batch = obs_batch.float() / 255.0
        else:
            obs_batch = torch.cuda.FloatTensor(obs_batch)

        if len(obs_batch.size()) == 4:
            obs_batch = obs_batch.permute((0, 3, 1, 2))
        return obs_batch

    def save(self, fn):
        algos_torch.torch_ext.save_scheckpoint(fn, self.epoch_num, self.model, self.optimizer)

    def restore(self, fn):
        self.epoch_num = algos_torch.torch_ext.load_checkpoint(fn, self.model, self.optimizer)

    def get_masked_action_values(self, obs, action_masks):
        obs = self._preproc_obs(obs)
        action_masks = torch.Tensor(action_masks).cuda()
        input_dict = {
            'is_train': False,
            'prev_actions': None, 
            'inputs' : obs,
            'action_masks' : action_masks
        }
        with torch.no_grad():
            neglogp, value, action, logits = self.model(input_dict)
        return action.detach().cpu().numpy(), value.detach().cpu().numpy(), neglogp.detach().cpu().numpy(), logits.detach().cpu().numpy(), None


    def get_action_values(self, obs):
        obs = self._preproc_obs(obs)
        self.model.eval()
        input_dict = {
            'is_train': False,
            'prev_actions': None, 
            'inputs' : obs,
        }
        with torch.no_grad():
            neglogp, value, action, logits = self.model(input_dict)
        return action.detach().cpu().numpy(), value.detach().cpu().numpy(), neglogp.detach().cpu().numpy(), None

    def get_values(self, obs):
        obs = self._preproc_obs(obs)
        self.model.eval()
        input_dict = {
            'is_train': False,
            'prev_actions': None, 
            'inputs' : obs
        }
        with torch.no_grad():
            neglogp, value, action, logits = self.model(input_dict)
        return value.detach().cpu().numpy()

    def get_weights(self):
        return torch.nn.utils.parameters_to_vector(self.model.parameters())
    
    def set_weights(self, weights):
        torch.nn.utils.vector_to_parameters(weights, self.model.parameters())

    def train_actor_critic(self, input_dict):
        self.model.train()
        value_preds_batch = torch.cuda.FloatTensor(input_dict['old_values'])
        old_action_log_probs_batch = torch.cuda.FloatTensor(input_dict['old_logp_actions'])
        advantage = torch.cuda.FloatTensor(input_dict['advantages'])
        return_batch = torch.cuda.FloatTensor(input_dict['returns'])
        actions_batch = torch.cuda.LongTensor(input_dict['actions'])
        obs_batch = input_dict['obs']
        obs_batch = self._preproc_obs(obs_batch)
        lr = self.last_lr
        kl = 1.0
        lr_mul = 1.0
        curr_e_clip = lr_mul * self.e_clip

        input_dict = {
            'is_train': True,
            'prev_actions': actions_batch, 
            'inputs' : obs_batch
        }
        action_log_probs, values, entropy = self.model(input_dict)

        if self.ppo:
            ratio = torch.exp(old_action_log_probs_batch - action_log_probs)
            surr1 = ratio * advantage
            surr2 = torch.clamp(ratio, 1.0 - curr_e_clip,
                                1.0 + curr_e_clip) * advantage
            a_loss = torch.max(-surr1, -surr2).mean()
        else:
            a_loss = (action_log_probs * advantage).mean()

        values = torch.squeeze(values)
        if self.clip_value:
            value_pred_clipped = value_preds_batch + \
                (values - value_preds_batch).clamp(-curr_e_clip, curr_e_clip)
            value_losses = (values - return_batch)**2
            value_losses_clipped = (value_pred_clipped - return_batch)**2
            c_loss = torch.max(value_losses,
                                         value_losses_clipped)
        else:
            c_loss = (return_batch - values)**2

        c_loss = c_loss.mean()
        loss = a_loss + 0.5 *c_loss * self.critic_coef - entropy * self.entropy_coef

        self.optimizer.zero_grad()
        loss.backward()
        nn.utils.clip_grad_norm_(self.model.parameters(), self.grad_norm)
        self.optimizer.step()
        with torch.no_grad():
            kl = 0.5 * ((old_action_log_probs_batch - action_log_probs)**2).mean()

        return a_loss.item(), c_loss.item(), entropy.item(), kl.item(), lr, lr_mul