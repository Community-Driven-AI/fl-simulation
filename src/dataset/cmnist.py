from argparse import Namespace
import time
from tokenize import group
from typing import Dict, Any
from torch.utils.data import Dataset
from PIL import Image
from torchvision import transforms, datasets
import numpy as np
import pandas as pd
from typing import Tuple, Dict
import torch
from functools import reduce
import sys
import os
current = os.path.dirname(os.path.realpath(__file__))
parent = os.path.dirname(current)
sys.path.append(parent)
sys.path.append('../models')
from metrics import prediction
import models
import time

class CMNISTDataset(Dataset):
    def __init__(self, transform, diversity_ratio:str, split:str):
        super(CMNISTDataset, self).__init__()
        self.dir = '../data/cmnist/'
        self.transform = transform
        if split == 'train':
            self.dir += diversity_ratio
        else:
            self.dir += 'test/'
        image_ids = []
        for path, subdirs, files in os.walk(self.dir):
            if 'valid' in path:
                continue
            for name in files:
                image_ids.append(os.path.join(path, name).replace(self.dir, ''))

        protected_variables = torch.tensor([int(image_id.split('_')[-1][0]) for image_id in image_ids])
        target = torch.tensor([int(image_id.split('_')[1]) for image_id in image_ids])
        self.image_ids = image_ids
        self.protected_variables = protected_variables
        self.labels = target

    def __getitem__(self, index: int) -> Tuple[Any, Any]:
        path = self.image_ids[index]
        target = self.labels[index]
        with open(self.dir + path, 'rb') as f:
            img = Image.open(f)
            sample = img.convert('RGB')
        if self.transform is not None:
            sample = self.transform(sample)
        return sample, target, index

    def __len__(self) -> int:
        return len(self.labels)

def get_dataset(split, args):
    apply_transform_train = transforms.Compose(
        [
            transforms.RandomAffine(degrees=45, translate=(0.1, 0.1), scale=(0.8, 1.2)),
            transforms.ToTensor(),
                                ])
    apply_transform_test = transforms.Compose([transforms.ToTensor()])

    apply_transform_train = apply_transform_test

    if split == 'train':
        transform = apply_transform_train
    else:
        transform = apply_transform_test
    
    dataset = CMNISTDataset(transform=transform, diversity_ratio=args.diversity_ratio, split=split)
    return dataset

def split_client_indices(dataset, args: Namespace) -> list:
    if args.distribution == 'iid':
        return sampling_iid(dataset, args.clients)
    if args.distribution == 'noniid':
        return sampling_noniid(dataset, args.clients, args.beta)
    if args.distribution == 'seperate':
        return sampling_seperate(dataset, args.clients)
    if args.distribution == 'seperate_multiple':
        return sampling_seperate_multiple(dataset)
    if args.distribution == 'weak':
        return sampling_weak(dataset)
    if args.distribution == 'jtt':
        model = models.get_model(args)
        weights = torch.load('../save/models/cub_fl_jtt.pt')
        model.load_state_dict(weights)
        pred_labels = prediction(model, dataset, torch.device('cuda:0'), args)
        return sampling_jtt(dataset, pred_labels)

def sampling_iid(dataset: CMNISTDataset, num_clients) -> list:
    subgroups = []
    for v in torch.unique(dataset.protected_variables):
        subgroups.append((dataset.protected_variables == v).nonzero().squeeze())
    client_indices = [[] for _ in range(num_clients)]
    for group_indices in subgroups:
        group_indices = group_indices[torch.randperm(len(group_indices))]
        splitted_indices = np.array_split(group_indices, num_clients)
        for c_i, s_i in zip(client_indices, splitted_indices):
            c_i.append(s_i)
    for i, indices in enumerate(client_indices):
        client_indices[i] = np.concatenate(indices, axis=0)
    return client_indices

def sampling_noniid(subgroups: Dict, num_clients, beta, min_size_bound=50) -> list:
    min_size = 0
    avg_samples = sum([len(group_indices) for group_indices in subgroups.values()]) / num_clients
    print('sampling as non iid, avg_sample is {}'.format(avg_samples))
    subgroup_keys = [list(k) for k in subgroups.keys()]
    each_attr = [list(set([x[i] for x in subgroup_keys])) for i in range(len(subgroup_keys[0]))]
    while min_size < min_size_bound:
        proportions_list = []
        for attrs in each_attr:
            proportions_list.append({})
            for i in attrs:
                proportions = np.random.dirichlet(np.repeat(beta, num_clients))
                proportions_list[-1][i] = proportions
        
        client_indices = [np.array([], dtype=np.int) for _ in range(num_clients)]
        for k, group_indices in subgroups.items():
            np.random.shuffle(group_indices)
            subgroup_proportion = [1 for _ in range(num_clients)]
            for i, j in enumerate(list(k)):
                subgroup_proportion = [a*b for a, b in zip(subgroup_proportion, proportions_list[i][j])]
            # Balance
            subgroup_proportion = np.sort(subgroup_proportion)
            subgroup_proportion = subgroup_proportion / subgroup_proportion.sum()
            subgroup_proportion = (np.cumsum(subgroup_proportion)*len(group_indices)).astype(int)[:-1]
            # print('subgroup: ', k)
            # print(subgroup_proportion)
            splitted_indices = np.split(group_indices, subgroup_proportion)
            client_sorted = np.argsort([len(x) for x in client_indices])
            for i, s_i in enumerate (splitted_indices):
                client_indices[client_sorted[-i-1]] = np.concatenate((client_indices[client_sorted[-i-1]], s_i), axis=0)
        min_size = min([len(indices) for indices in client_indices])
    return client_indices

def sampling_weak(dataset: CMNISTDataset) -> list:
    subgroups = []
    subgroups.append(torch.cat([((dataset.protected_variables == 0) & (dataset.labels == 0)).nonzero().squeeze(), ((dataset.protected_variables == 1) & (dataset.labels == 1)).nonzero().squeeze()]))
    subgroups.append(torch.cat([((dataset.protected_variables == 0) & (dataset.labels == 1)).nonzero().squeeze(), ((dataset.protected_variables == 1) & (dataset.labels == 0)).nonzero().squeeze()]))
    return subgroups

def sampling_jtt(dataset: CMNISTDataset, pred_labels) -> list:
    pred_labels = pred_labels.cpu()
    print(torch.ne(pred_labels, dataset.labels).count_nonzero())
    wrong_sets = [((pred_labels != dataset.labels) & (dataset.labels == 0)).nonzero().squeeze(), ((pred_labels != dataset.labels) & (dataset.labels == 1)).nonzero().squeeze()]
    corr_sets = [((pred_labels == dataset.labels) & (dataset.labels == 0)).nonzero().squeeze(), ((pred_labels == dataset.labels) & (dataset.labels == 1)).nonzero().squeeze()]
    subgroups = [torch.cat([wrong_sets[0], wrong_sets[1]]), torch.cat([corr_sets[0], wrong_sets[1]]), torch.cat([corr_sets[1], wrong_sets[0]])]
    wrong_set_male = [((pred_labels != dataset.labels) & (dataset.labels == 0) & (dataset.protected_variables == 1)).nonzero().squeeze(), ((pred_labels != dataset.labels) & (dataset.labels == 1) & (dataset.protected_variables == 1)).nonzero().squeeze()]
    wrong_set_female = [((pred_labels != dataset.labels) & (dataset.labels == 0) & (dataset.protected_variables == 0)).nonzero().squeeze(), ((pred_labels != dataset.labels) & (dataset.labels == 1) & (dataset.protected_variables == 0)).nonzero().squeeze()]
    print("wrong_set_male sets", [len(a) for a in wrong_set_male])
    print("wrong_set_female sets", [len(a) for a in wrong_set_female])

    return subgroups

def sampling_seperate(dataset: CMNISTDataset, num_clients) -> list:
    subgroups = []
    for v in torch.unique(dataset.protected_variables):
        # for _ in range(num_clients // len(torch.unique(dataset.protected_variables))):
        subgroups.append((dataset.protected_variables == v).nonzero().squeeze())
    
    # bias_conflicting_samples = []
    # for l in torch.unique(dataset.labels):
    #     max_v = np.argmax([((dataset.protected_variables == v) & (dataset.labels == l)).count_nonzero() for v in torch.unique(dataset.protected_variables)])
    #     bias_conflict = ((dataset.protected_variables != max_v) & (dataset.labels == l)).nonzero().squeeze()
    #     bias_conflicting_samples.append(bias_conflict)
    
    # subgroups.append(torch.concat(bias_conflicting_samples))

    print(len(subgroups))

    return subgroups

def sampling_seperate_multiple(dataset: CMNISTDataset) -> list:
    subgroups = []
    for v in torch.unique(dataset.protected_variables):
        label_samples = [((dataset.protected_variables == v) & (dataset.labels == l)).count_nonzero() for l in torch.unique(dataset.labels)]
        min_label = torch.unique(dataset.labels)[torch.argmin(torch.tensor(label_samples))]
        tmp_subgroups = [[] for _ in range(max(label_samples) // min(label_samples) + 1)]
        for l in torch.unique(dataset.labels):
            if l != min_label:
                for a,b in zip(tmp_subgroups, ((dataset.protected_variables == v) & (dataset.labels == l)).nonzero().squeeze().split(min(label_samples))):
                    a.append(b)
            else:
                for a in tmp_subgroups:
                    a.append(((dataset.protected_variables == v) & (dataset.labels == l)).nonzero().squeeze())

        subgroups += [torch.cat(a) for a in tmp_subgroups]
    print([len(a) for a in subgroups])
    return subgroups