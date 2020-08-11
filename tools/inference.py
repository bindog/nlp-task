from __future__ import absolute_import, division, print_function

import logging
import os
import sys
import time
import json
import datetime
import shutil
import numpy as np
import torch
import torch.nn.functional as F
from tools import official_tokenization as tokenization, utils
from torch.utils.data import (DataLoader, RandomSampler, SequentialSampler,
                              TensorDataset)

from modeling_nezha import (BertForSequenceClassification, BertForTokenClassification,
                            BertForMultiLabelingClassification, BertConfig, WEIGHTS_NAME, CONFIG_NAME)

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(name)s -   %(message)s',
                    datefmt='%m/%d/%Y %H:%M:%S',
                    level=logging.INFO)
logger = logging.getLogger(__name__)


class MultiLabelingInferenceService(object):
    def __init__(self, model_dir):
        with open(os.path.join(model_dir, "label_map")) as f:
            label_map = json.loads(f.read().strip())
            self.label_map = {int(k):v for k, v in label_map.items()}
        num_labels = len(self.label_map)
        self.tokenizer = tokenization.BertTokenizer(vocab_file=os.path.join(model_dir, 'vocab.txt'), do_lower_case=True)
        config = BertConfig(os.path.join(model_dir, 'bert_config.json'))
        self.model = BertForMultiLabelingClassification(config, num_labels=num_labels)
        self.model.load_state_dict(torch.load(os.path.join(model_dir, WEIGHTS_NAME)))
        self.model.cuda()
        self.model.eval()

    def input_preprocess(self, text, max_seq_length=128):
        if len(text) > max_seq_length:
            # split or other way?
            pass
        else:
            tokens_a = self.tokenizer.tokenize(text)
            tokens_a = tokens_a[:max_seq_length - 2]
            tokens = ["[CLS]"] + tokens_a + ["[SEP]"]
            segment_ids = [0] * len(tokens)
            input_ids = self.tokenizer.convert_tokens_to_ids(tokens)
            input_mask = [1] * len(input_ids)

            input_ids = torch.tensor([input_ids], dtype=torch.long)
            input_mask = torch.tensor([input_mask], dtype=torch.long)
            segment_ids = torch.tensor([segment_ids], dtype=torch.long)
            return input_ids, input_mask, segment_ids

    def parse_label(self, preds):
        # for single batch inference
        idx = preds.nonzero()[1]
        labels = [self.label_map[i] for i in idx]
        return labels

    def inference(self, text, thresh=0.5):
        input_ids, input_mask, segment_ids = self.input_preprocess(text)
        with torch.no_grad():
            input_ids = input_ids.cuda()
            input_mask = input_mask.cuda()
            segment_ids = segment_ids.cuda()
            logits = self.model(input_ids, segment_ids, input_mask)
            preds = (logits.detach().cpu().sigmoid() > 0.5).byte().numpy()
            return self.parse_label(preds)


class NERInferenceService(object):
    def __init__(self, model_dir):
        label_list = ["O", "B-PER", "I-PER", "B-ORG", "I-ORG", "B-LOC", "I-LOC",
                        "B-PRO", "I-PRO", "B-JOB", "I-JOB", "B-TIME", "I-TIME",
                        "B-COM", "I-COM", "X", "[CLS]", "[SEP]"]
        self.label_map_reverse = {i : label for i, label in enumerate(label_list,1)}
        num_labels = len(label_list) + 1
        self.mapping_dict = {
            "PER": "人名",
            "LOC": "地名",
            "ORG": "组织",
            "JOB": "组织",
            "PRO": "产品",
            "TIME": "时间",
            "COM": "公司"
        }
        self.tokenizer = tokenization.BertTokenizer(vocab_file=os.path.join(model_dir, 'vocab.txt'), do_lower_case=True)
        config = BertConfig(os.path.join(model_dir, 'bert_config.json'))
        self.model = BertForTokenClassification(config, num_labels=num_labels)
        self.model.load_state_dict(torch.load(os.path.join(model_dir, WEIGHTS_NAME)))
        self.model.cuda()
        self.model.eval()

    def input_preprocess(self, text, max_seq_length=128):
        if len(text) > max_seq_length:
            # split or other way?
            pass
        else:
            tokens = []
            valid = []
            for i, word in enumerate(text):
                token = self.tokenizer.tokenize(word)
                tokens.extend(token)
                for m in range(len(token)):
                    if m == 0:
                        valid.append(1)
                    else:
                        valid.append(0)
            if len(tokens) >= max_seq_length - 1:
                tokens = tokens[0:(max_seq_length - 2)]
                valid = valid[0:(max_seq_length - 2)]
            ntokens = []
            segment_ids = []
            ntokens.append("[CLS]")
            segment_ids.append(0)
            for i, token in enumerate(tokens):
                ntokens.append(token)
                segment_ids.append(0)
            ntokens.append("[SEP]")
            segment_ids.append(0)
            input_ids = self.tokenizer.convert_tokens_to_ids(ntokens)
            input_mask = [1] * len(input_ids)
            while len(input_ids) < max_seq_length:
                input_ids.append(0)
                input_mask.append(0)
                segment_ids.append(0)
            assert len(input_ids) == max_seq_length
            assert len(input_mask) == max_seq_length
            assert len(segment_ids) == max_seq_length

            input_ids = torch.tensor([input_ids], dtype=torch.long)
            input_mask = torch.tensor([input_mask], dtype=torch.long)
            segment_ids = torch.tensor([segment_ids], dtype=torch.long)
            return input_ids, input_mask, segment_ids

    def parse_label(self, text, label):
        if label[0] != "[CLS]":
            print("wrong result in first label")
            return None

        entity_list = []
        index = 0
        entity_type = None
        entity_start = None
        entity_end = None
        for i, tag in enumerate(label[1:]):
            if tag in ["B-PER", "B-ORG", "B-LOC", "B-PRO", "B-JOB", "B-TIME", "B-COM"]:
                if entity_type is not None:
                    entity_end = index
                    entity_list.append((entity_start, entity_end, text[entity_start: entity_end], entity_type))
                entity_type = tag[2:]
                entity_start = index
            elif tag in ["I-PER", "I-ORG", "I-LOC", "I-PRO", "I-JOB", "I-TIME", "I-COM"]:
                if tag[2:] != entity_type:
                    print("wrong result in middle parsing...")
            else:
                if entity_type is not None:
                    entity_end = index
                    entity_list.append((entity_start, entity_end, text[entity_start: entity_end], entity_type))
                    entity_type = None
            index += 1
        return entity_list

    def inference(self, text):
        input_ids, input_mask, segment_ids = self.input_preprocess(text)
        with torch.no_grad():
            input_ids = input_ids.cuda()
            input_mask = input_mask.cuda()
            segment_ids = segment_ids.cuda()
            logits = self.model(input_ids, segment_ids, input_mask)
        logits = torch.argmax(F.log_softmax(logits, dim=2), dim=2)
        logits = logits.detach().cpu().numpy()
        label = [self.label_map_reverse[i] for i in logits[0]]
        entity_list = self.parse_label(text, label)

        result = []
        for r in entity_list:
            rj = {}
            rj["entity"] = r[2]
            rj["type"] = self.mapping_dict[r[3]]
            rj["offset"] = [r[0], r[1]]
            result.append(rj)
        return result
