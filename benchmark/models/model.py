from __future__ import annotations
from abc import abstractmethod
from ConfigSpace import ConfigurationSpace

from benchmark.datasets.dataset import Dataset


class Model:
    def __init__(self, dataset: Dataset):
        self._dataset = dataset
        
    @property
    def dataset(self) -> Dataset:
        return self._dataset
        
    @abstractmethod
    @property
    def configspace(self) -> ConfigurationSpace:
        raise NotImplementedError


class SingleObjectiveModel(Model):
    @abstractmethod
    def train(self) -> float:
        raise NotImplementedError


class MultiObjectiveModel(Model):
    @abstractmethod
    def train(self) -> list[float]:
        raise NotImplementedError
