import torch
import numpy as np
import random

from types import SimpleNamespace
from typing import List
from tqdm import tqdm
from functools import lru_cache

from ..trainers.QA_system_loader import SystemLoader
from ..trainers.QG_system_loader import QgSystemLoader

from ..utils.torch_utils import no_grad
from ..data_utils.data_loader import QaDataLoader
from ..batchers.QA_batcher import QaBatcher
from ..utils.general import get_base_dir, join_paths

class AdversarialOptionSearcher(SystemLoader):
    def __init__(self, exp_path, device=None):
        super().__init__(exp_path)
        self.set_up_helpers(device)
        self.convert_dataloader()
            
    def convert_dataloader(self):
        self.data_loader.__class__ = AdversarialOptionDataLoader
    
    @no_grad
    def _probs(self, data_name:str, mode='test'):
        """get imposter model predictions for given data"""
        self.model.eval()
        self.to(self.device)
        
        #this code mimics the internals of dataloader
        data = self.data_loader.load_split(data_name, mode, lim)
        eval_data = ._prep_MCRC_ids(data)
    
        eval_data = self.data_loader.prep_MCRC_split(data_name, mode)
        eval_batches = self.batcher(data=eval_data, bsz=1, shuffle=False)
        
        probabilties = {}
        for batch in tqdm(eval_batches):
            ex_id = batch.ex_id[0]
            output = self.model_output(batch)

            logits = output.logits.squeeze(0)
            if logits.shape and logits.shape[-1] > 1:  # Get probabilities of predictions
                prob = F.softmax(logits, dim=-1)
            probabilties[ex_id] = prob.cpu().numpy()
        return probabilties
    
    @no_grad
    def find_adversarial_options(self, data_name, lim=None ,num_adv=None):
        prep_inputs, options, num_q = self.data_loader.find_magnet_options(data_name, lim, num_adv)
        
        matrix = np.zeros((num_q, len(options)+1))
        
        for ex in prep_inputs:
            input_ids = ex.input_ids.to(self.device)
            h = self.model.electra(input_ids)[0]
            pooled_output = self.model.sequence_summary(h)
            logit_score = self.model.classifier(pooled_output)
            matrix[ex.q_num, ex.opt_num] = logit_score
        return matrix, options
        
class AdversarialOptionDataLoader(QaDataLoader):
    
    #== Adversarial Magnet Options Search ====================================================================
    def find_magnet_options(self, data_name, lim=None, num_adv=None):
        """ Finds options which universally get selected by the system"""
        train, dev, test = self.load_data(data_name)
        dev = self.rand_select(dev, lim) 
        
        #select random 5000 (num_adv) answers
        option_list = self.get_all_answers(train)
        if num_adv:
            rng = random.Random(1)
            option_list = rng.sample(option_list, num_adv)
        option_list = list(set(option_list))
        
        #produce the actual model inputs
        tok_model_inputs = self.prep_magnet_search(dev, option_list)
        return tok_model_inputs, option_list, len(dev)
        
    def prep_magnet_search(self, data, options):
        """given questions and options, creates all permutations for model inputs"""
        tokenized_inputs = []
        for q_num, ex in tqdm(enumerate(data), total=len(data)):
            Q_ids = self.tokenizer(ex.question).input_ids
            C_ids = self.cache_tokenize(ex.context)
            answer = ex.options[ex.answer]
            
            for k, option_text in enumerate([answer]+options):
                O_ids = self.tokenizer(option_text).input_ids
                ids = self._prep_single_option(Q_ids, C_ids, O_ids)
                if len(ids) > 512: 
                    ids = [ids[0]] + ids[-511:]
                input_ids = torch.LongTensor([ids])
                ex = SimpleNamespace(q_num=q_num, opt_num=k, input_ids=input_ids)
                yield ex

    #== Adversarial Imposter Option Evaluation ====================================================================
    default_path = '../../investigations/QG/trained_models' 
    def load_imposter_data(self, data_name, imposter_path=default_path, split='test', lim=None):
        imposter_system = QgSystemLoader(imposter_path)
        imposter_system.to(device)

        data = self.load_data_split(data_name, split)
        random.seed(1)        

        for ex in data:
            #for each example replace a random option with the imposter option
            imposter_option = imposter_system.generate_option(ex=ex)
            rand_opt = random.choice([i for i in range(len(options)) if i != ex.answer])
            ex.options[rand_opt] = imposter_option 
        return data
    
    #== General util functions =================================================================================
    def _prep_single_option(self, Q_ids:List[int], C_ids:List[int], O_ids:List[int]):
        if self.formatting == 'standard':
            ids = C_ids + Q_ids[1:-1] + O_ids[1:]
        elif self.formatting == 'O':
            ids = O_ids
        elif self.formatting == 'QO':
            ids = Q_ids[:-1] + O_ids[1:] 
        elif self.formatting == 'CO':
            ids = C_ids + O_ids[1:]
        return ids
    
    @staticmethod
    def get_all_options(data):
        all_options = set()
        for ex in data:
            all_options.update(ex.options)
        return list(all_options)

    @staticmethod
    def get_all_answers(data):
        all_options = []
        for ex in data:
            answer = ex.options[ex.answer]
            all_options.append(answer)
        return all_options
    
    @lru_cache(maxsize=1000000)
    def cache_tokenize(self, text):
        return self.tokenizer(text).input_ids
       