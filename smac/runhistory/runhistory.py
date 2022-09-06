from __future__ import annotations
from pathlib import Path

from typing import Any, Iterable, Iterator, Mapping, cast

import json
from collections import OrderedDict

import numpy as np

from ConfigSpace import ConfigurationSpace, Configuration
from smac.multi_objective.utils import normalize_costs
from smac.runhistory.dataclasses import (
    InstanceSeedBudgetKey,
    InstanceSeedKey,
    TrialKey,
    TrialValue,
)
from smac.runhistory.enumerations import DataOrigin, StatusType
from smac.utils.logging import get_logger

__copyright__ = "Copyright 2022, automl.org"
__license__ = "3-clause BSD"

logger = get_logger(__name__)


class RunHistory(Mapping[TrialKey, TrialValue]):
    """Container for the target algorithm run information.

    Most importantly, the runhistory contains an efficient mapping from each evaluated configuration to the
    empirical cost observed on either the full instance set or a subset. The cost is the average over all
    observed costs for one configuration:

    * If using budgets for a single instance, only the cost on the highest observed budget is returned.
    * If using instances as the budget, the average cost over all evaluated instances is returned.
    * Theoretically, the runhistory object can handle instances and budgets at the same time. This is
      neither used nor tested.

    Note
    ----
    Guaranteed to be picklable.

    Parameters
    ----------
    overwrite_existing_trials : bool, defaults to false
        Overwrites a trial (combination of configuration, instance, budget and seed) if it already exists.
    """

    def __init__(self, overwrite_existing_trials: bool = False) -> None:
        self._overwrite_existing_trials = overwrite_existing_trials
        self.reset()

    @property
    def ids_config(self) -> dict[int, Configuration]:
        """Mapping from config id to configuration."""
        return self._ids_config

    @property
    def config_ids(self) -> dict[Configuration, int]:
        """Mapping from configuration to config id."""
        return self._config_ids

    @property
    def objective_bounds(self) -> list[tuple[float, float]]:
        """Returns the lower and upper bound of each objective."""
        return self._objective_bounds

    def reset(self) -> None:
        """Resets this runhistory to it's default state."""
        # By having the data in a deterministic order we can do useful tests when we
        # serialize the data and can assume it's still in the same order as it was added.
        self._data: dict[TrialKey, TrialValue] = OrderedDict()

        # For fast access, we have also an unordered data structure to get all instance
        # seed pairs of a configuration.
        self._config_id_to_isk_to_budget: dict[int, dict[InstanceSeedKey, list[float | None]]] = {}

        self._config_ids: dict[Configuration, int] = {}
        self._ids_config: dict[int, Configuration] = {}
        self._n_id = 0

        # Stores cost for each configuration ID
        self._cost_per_config: dict[int, float | list[float]] = {}
        # Stores min cost across all budgets for each configuration ID
        self._min_cost_per_config: dict[int, float | list[float]] = {}
        # Maps the configuration ID to the number of runs for that configuration
        # and is necessary for computing the moving average.
        self._num_trials_per_config: dict[int, int] = {}

        # Store whether a datapoint is "external", which means it was read from
        # a JSON file. Can be chosen to not be written to disk.
        self._external: dict[TrialKey, DataOrigin] = {}
        self._n_objectives: int = -1
        self._objective_bounds: list[tuple[float, float]] = []

    def __contains__(self, k: object) -> bool:
        """Dictionary semantics for `k in runhistory`."""
        return k in self._data

    def __getitem__(self, k: TrialKey) -> TrialValue:
        """Dictionary semantics for `v = runhistory[k]`."""
        return self._data[k]

    def __iter__(self) -> Iterator[TrialKey]:
        """Dictionary semantics for `for k in runhistory.keys()`."""
        return iter(self._data.keys())

    def __len__(self) -> int:
        """Enables the `len(runhistory)`"""
        return len(self._data)

    def __eq__(self, other: Any) -> bool:
        """Enables to check equality of runhistory if the run is continued."""
        return self._data == other._data

    def empty(self) -> bool:
        """Check whether or not the RunHistory is empty.

        Returns
        -------
        emptiness: bool
            True if trials have been added to the RunHistory.
        """
        return len(self._data) == 0

    def add(
        self,
        config: Configuration,
        cost: int | float | list[int | float],
        time: float,
        status: StatusType = StatusType.SUCCESS,
        instance: str | None = None,
        seed: int | None = None,
        budget: float | None = None,
        starttime: float = 0.0,
        endtime: float = 0.0,
        additional_info: dict[str, Any] = {},
        origin: DataOrigin = DataOrigin.INTERNAL,
        force_update: bool = False,
    ) -> None:
        """Adds a new trial.

        Parameters
        ----------
        config : Configuration
        cost : int | float | list[int | float]
            Cost of the evaluated trial. Might be a list in case of multi-objective.
        time : float
            How much time was needed to evaluate the trial.
        status : StatusType, defaults to StatusType.SUCCESS
            The status of the trial.
        instance : str | None, defaults to none
        seed : int | None, defaults to none
        budget : float | None, defaults to none
        starttime : float, defaults to 0.0
        endtime : float, defaults to 0.0
        additional_info : dict[str, Any], defaults to {}
        origin : DataOrigin, defaults to DataOrigin.INTERNAL
        force_update : bool, defaults to false
            Overwrites a previous trial if the trial already exists.
        """
        if config is None:
            raise TypeError("Configuration must not be None.")
        elif not isinstance(config, Configuration):
            raise TypeError("Configuration is not of type Configuration, but %s." % type(config))

        # Squeeze is important to reduce arrays with one element
        # to scalars.
        cost_array = np.asarray(cost).squeeze()
        n_objectives = np.size(cost_array)

        # Get the config id
        config_id = self._config_ids.get(config)

        if config_id is None:
            self._n_id += 1
            self._config_ids[config] = self._n_id
            self._ids_config[self._n_id] = config

            config_id = self._n_id

        if self._n_objectives == -1:
            self._n_objectives = n_objectives
        elif self._n_objectives != n_objectives:
            raise ValueError(
                f"Cost is not of the same length ({n_objectives}) as the number of "
                f" objectives ({self._n_objectives})"
            )

        # Let's always work with floats; Makes it easier to deal with later on
        # array.tolist(), it returns a scalar if the array has one element.
        c = cost_array.tolist()
        if self._n_objectives == 1:
            c = float(c)
        else:
            c = [float(i) for i in c]

        if budget is not None:
            # Just to make sure we really add a float
            budget = float(budget)

        k = TrialKey(config_id=config_id, instance=instance, seed=seed, budget=budget)
        v = TrialValue(
            cost=c,
            time=time,
            status=status,
            starttime=starttime,
            endtime=endtime,
            additional_info=additional_info,
        )

        # Construct keys and values for the data dictionary
        for key, value in (
            ("config", config.get_dictionary()),
            ("config_id", config_id),
            ("instance", instance),
            ("seed", seed),
            ("budget", budget),
            ("cost", c),
            ("time", time),
            ("status", status),
            ("starttime", starttime),
            ("endtime", endtime),
            ("additional_info", additional_info),
            ("origin", config.origin),
        ):
            self._check_json_serializable(key, value, k, v)

        # Each trial_key is supposed to be used only once. Repeated tries to add
        # the same trial_key will be ignored silently if not capped.
        if self._overwrite_existing_trials or force_update or self._data.get(k) is None:
            self._add(k, v, status, origin)
        else:
            logger.info("Entry was not added to the runhistory because existing trials will not be overwritten.")

    def update_cost(self, config: Configuration) -> None:
        """Stores the performance of a configuration across the instances in `self._cost_per_config`
        and also updates `self._num_trials_per_config`.

        Parameters
        ----------
        config: Configuration
            configuration to update cost based on all trials in runhistory
        """
        config_id = self._config_ids[config]

        # Removing duplicates while keeping the order
        inst_seed_budgets = list(dict.fromkeys(self.get_trials(config, only_max_observed_budget=True)))
        self._cost_per_config[config_id] = self.average_cost(config, inst_seed_budgets)
        self._num_trials_per_config[config_id] = len(inst_seed_budgets)

        all_isb = list(dict.fromkeys(self.get_trials(config, only_max_observed_budget=False)))
        self._min_cost_per_config[config_id] = self.min_cost(config, all_isb)

    def incremental_update_cost(self, config: Configuration, cost: float | list[float]) -> None:
        """Incrementally updates the performance of a configuration by using a moving average.

        Parameters
        ----------
        config: Configuration
            configuration to update cost based on all trials in runhistory
        cost: float
            cost of new run of config
        """
        config_id = self._config_ids[config]
        n_trials = self._num_trials_per_config.get(config_id, 0)

        if self._n_objectives > 1:
            costs = np.array(cost)
            old_costs = self._cost_per_config.get(config_id, np.array([0.0 for _ in range(self._n_objectives)]))
            old_costs = np.array(old_costs)

            new_costs = ((old_costs * n_trials) + costs) / (n_trials + 1)
            self._cost_per_config[config_id] = new_costs.tolist()
        else:
            old_cost = self._cost_per_config.get(config_id, 0.0)

            assert isinstance(cost, float)
            assert isinstance(old_cost, float)
            self._cost_per_config[config_id] = ((old_cost * n_trials) + cost) / (n_trials + 1)

        self._num_trials_per_config[config_id] = n_trials + 1

    def get_cost(self, config: Configuration) -> float:
        """Returns empirical cost for a configuration. See the class docstring for how the costs are
        computed. The costs are not re-computed, but are read from cache.

        Parameters
        ----------
        config: Configuration

        Returns
        -------
        cost: float
            Computed cost for configuration
        """
        config_id = self._config_ids.get(config)

        # Cost is always a single value (Single objective) or a list of values (Multi-objective)
        # For example, _cost_per_config always holds the value on the highest budget
        cost = self._cost_per_config.get(config_id, np.nan)  # type: ignore[arg-type] # noqa F821

        if self._n_objectives > 1:
            assert isinstance(cost, list)

            # We have to normalize the costs here
            costs = normalize_costs(cost, self._objective_bounds)
            return float(np.mean(costs))

        assert isinstance(cost, float)
        return float(cost)

    def get_min_cost(self, config: Configuration) -> float:
        """Returns the lowest empirical cost for a configuration across all trials.

        See the class docstring for how the costs are computed. The costs are not re-computed
        but are read from cache.

        Parameters
        ----------
        config : Configuration

        Returns
        -------
        min_cost: float
            Computed cost for configuration
        """
        config_id = self._config_ids.get(config)
        cost = self._min_cost_per_config.get(config_id, np.nan)  # type: ignore

        if self._n_objectives > 1:
            assert type(cost) == list
            costs = normalize_costs(cost, self._objective_bounds)

            # Note: We have to mean here because we already got the min cost
            return float(np.mean(costs))

        assert type(cost) == float
        return float(cost)

    def average_cost(
        self,
        config: Configuration,
        instance_seed_budget_keys: Iterable[InstanceSeedBudgetKey] | None = None,
        normalize: bool = False,
    ) -> float | list[float]:
        """Return the average cost of a configuration. This is the mean of costs of all instance-
        seed pairs.

        Parameters
        ----------
        config : Configuration
            Configuration to calculate objective for.
        instance_seed_budget_keys : list, optional (default=None)
            List of tuples of instance-seeds-budget keys. If None, the runhistory is
            queried for all trials of the given configuration.
        normalize : bool, optional (default=False)
            Normalizes the costs wrt objective bounds in the multi-objective setting.
            Only a float is returned if normalize is True. Warning: The value can change
            over time because the objective bounds are changing.

        Returns
        -------
        Cost: float | list[float]
            Average cost. In case of multiple objectives, the mean of each objective is returned.
        """
        costs = self._cost(config, instance_seed_budget_keys)
        if costs:
            if self._n_objectives > 1:
                # Each objective is averaged separately
                # [[100, 200], [0, 0]] -> [50, 100]
                averaged_costs = np.mean(costs, axis=0).tolist()

                if normalize:
                    normalized_costs = normalize_costs(averaged_costs, self._objective_bounds)
                    return float(np.mean(normalized_costs))
                else:
                    return averaged_costs

            return float(np.mean(costs))

        return np.nan

    def sum_cost(
        self,
        config: Configuration,
        instance_seed_budget_keys: Iterable[InstanceSeedBudgetKey] | None = None,
        normalize: bool = False,
    ) -> float | list[float]:
        """Return the sum of costs of a configuration. This is the sum of costs of all instance-seed
        pairs.

        Parameters
        ----------
        config : Configuration
            Configuration to calculate objective for.
        instance_seed_budget_keys : list, optional (default=None)
            List of tuples of instance-seeds-budget keys. If None, the runhistory is
            queried for all trials of the given configuration.
        normalize : bool, optional (default=False)
            Normalizes the costs wrt objective bounds in the multi-objective setting.
            Only a float is returned if normalize is True. Warning: The value can change
            over time because the objective bounds are changing.

        Returns
        -------
        sum_cost: float | list[float]
            Sum of costs of config. In case of multiple objectives, the costs are summed up for each
            objective individually.
        """
        costs = self._cost(config, instance_seed_budget_keys)
        if costs:
            if self._n_objectives > 1:
                # Each objective is summed separately
                # [[100, 200], [20, 10]] -> [120, 210]
                summed_costs = np.sum(costs, axis=0).tolist()

                if normalize:
                    normalized_costs = normalize_costs(summed_costs, self._objective_bounds)
                    return float(np.mean(normalized_costs))
                else:
                    return summed_costs

        return float(np.sum(costs))

    def min_cost(
        self,
        config: Configuration,
        instance_seed_budget_keys: Iterable[InstanceSeedBudgetKey] | None = None,
        normalize: bool = False,
    ) -> float | list[float]:
        """Return the minimum cost of a configuration.

        This is the minimum cost of all instance-seed pairs.

        Warning
        -------
        In the case of multi-fidelity, the minimum cost per objectives is returned.

        Parameters
        ----------
        config : Configuration
            Configuration to calculate objective for.
        instance_seed_budget_keys : list, optional (default=None)
            List of tuples of instance-seeds-budget keys. If None, the runhistory is
            queried for all trials of the given configuration.

        Returns
        -------
        min_cost: float | list[float]
            Minimum cost of the config. In case of multi-objective, the minimum cost per objective
            is returned.
        """
        costs = self._cost(config, instance_seed_budget_keys)
        if costs:
            if self._n_objectives > 1:
                # Each objective is viewed separately
                # [[100, 200], [20, 500]] -> [20, 200]
                min_costs = np.min(costs, axis=0).tolist()

                if normalize:
                    normalized_costs = normalize_costs(min_costs, self._objective_bounds)
                    return float(np.mean(normalized_costs))
                else:
                    return min_costs

            return float(np.min(costs))

        return np.nan

    def get_trials(
        self,
        config: Configuration,
        only_max_observed_budget: bool = True,
    ) -> list[InstanceSeedBudgetKey]:
        """Return all trials (instance seed budget key in this case) for a configuration.

        Parameters
        ----------
        config : Configuration
            Parameter configuration
        only_max_observed_budget : bool
            Select only the maximally observed budget run for this configuration

        Returns
        -------
        trials : list[InstanceSeedBudgetKey]
        """
        config_id = self._config_ids.get(config)
        trials = {}
        if config_id in self._config_id_to_isk_to_budget:
            trials = self._config_id_to_isk_to_budget[config_id].copy()

        # Select only the max budget run if specified
        if only_max_observed_budget:
            for k, v in trials.items():
                if None in v:
                    trials[k] = [None]
                else:
                    trials[k] = [max([v_ for v_ in v if v_ is not None])]

        # Convert to instance-seed-budget key
        return [InstanceSeedBudgetKey(k.instance, k.seed, budget) for k, v in trials.items() for budget in v]

    def get_configs(self) -> list[Configuration]:
        """Return all configurations in this RunHistory object.

        Returns
        -------
        parameter configurations: list
        """
        return list(self._config_ids.keys())

    def get_configs_per_budget(
        self,
        budget_subset: list | None = None,
    ) -> list[Configuration]:
        """Return all configs in this RunHistory object that have been run on one of these budgets.

        Parameters
        ----------
        budget_subset: list

        Returns
        -------
        parameter configurations: list
        """
        if budget_subset is None:
            return self.get_configs()

        configs = []
        for key in self._data.keys():
            if key.budget in budget_subset:
                configs.append(self._ids_config[key.config_id])

        return configs

    def get_incumbent(self) -> Configuration | None:
        """Returns the incumbent configuration. The config with the lowest cost calculated by `get_cost` is returned."""
        incumbent = None
        lowest_cost = np.inf
        for config in self._config_ids.keys():
            cost = self.get_cost(config)
            if cost < lowest_cost:
                incumbent = config
                lowest_cost = cost

        return incumbent

    def save_json(self, filename: str = "runhistory.json", save_external: bool = False) -> None:
        """Saves runhistory on disk.

        Parameters
        ----------
        filename : str
            file name.
        save_external : bool
            Whether to save external data in the runhistory file.
        """
        data = []
        for k, v in self._data.items():
            if save_external or self._external[k] == DataOrigin.INTERNAL:
                data += [
                    (
                        int(k.config_id),
                        str(k.instance) if k.instance is not None else None,
                        int(k.seed) if k.seed is not None else None,
                        float(k.budget) if k.budget is not None else None,
                        v.cost,
                        v.time,
                        v.status,
                        v.starttime,
                        v.endtime,
                        v.additional_info,
                    )
                ]

        config_ids_to_serialize = set([entry[0] for entry in data])
        configs = {}
        config_origins = {}
        for id_, config in self._ids_config.items():
            if id_ in config_ids_to_serialize:
                configs[id_] = config.get_dictionary()

            config_origins[id_] = config.origin

        # Some sanity-checks
        assert filename.endswith(".json")
        path = Path(filename)
        path.parent.mkdir(parents=True, exist_ok=True)

        with open(filename, "w") as fp:
            json.dump(
                {
                    "data": data,
                    "configs": configs,
                    "config_origins": config_origins,
                },
                fp,
                indent=2,
            )

    def load_json(self, filename: str, configspace: ConfigurationSpace) -> None:
        """Load and runhistory in json representation from disk.

        Warning
        -------
        Overwrites current runhistory!

        Parameters
        ----------
        filename : str
            file name to load from
        configspace : ConfigSpace
            instance of configuration space
        """
        try:
            with open(filename) as fp:
                all_data = json.load(fp)
        except Exception as e:
            logger.warning(
                f"Encountered exception {e} while reading runhistory from {filename}. Not adding any trials!"
            )
            return

        config_origins = all_data.get("config_origins", {})

        self._ids_config = {}
        for id_, values in all_data["configs"].items():
            self._ids_config[int(id_)] = Configuration(
                configspace,
                values=values,
                origin=config_origins.get(id_, None),
            )

        self._config_ids = {config: id_ for id_, config in self._ids_config.items()}
        self._n_id = len(self._config_ids)

        # Important to use add method to use all data structure correctly
        for entry in all_data["data"]:
            # Set n_objectives first
            if self._n_objectives == -1:
                if isinstance(entry[4], float) or isinstance(entry[4], int):
                    self._n_objectives = 1
                else:
                    self._n_objectives = len(entry[4])

            cost: list[float] | float
            if self._n_objectives == 1:
                cost = float(entry[4])
            else:
                cost = [float(x) for x in entry[4]]

            self.add(
                config=self._ids_config[int(entry[0])],
                cost=cost,
                time=float(entry[5]),
                status=StatusType(entry[6]),
                instance=entry[1],
                seed=entry[2],
                budget=entry[3],
                starttime=entry[7],
                endtime=entry[8],
                additional_info=entry[9],
            )

    def update_from_json(
        self,
        filename: str,
        configspace: ConfigurationSpace,
        origin: DataOrigin = DataOrigin.EXTERNAL_SAME_INSTANCES,
    ) -> None:
        """Updates the current runhistory by adding new trials from a json file.

        Parameters
        ----------
        filename : str
            File name to load from.
        configspace : ConfigurationSpace
        origin : DataOrigin, defaults to DataOrigin.EXTERNAL_SAME_INSTANCES
            What to store as data origin.
        """
        new_runhistory = RunHistory()
        new_runhistory.load_json(filename, configspace)
        self.update(runhistory=new_runhistory, origin=origin)

    def update(
        self,
        runhistory: RunHistory,
        origin: DataOrigin = DataOrigin.EXTERNAL_SAME_INSTANCES,
    ) -> None:
        """Updates the current runhistory by adding new trials from a RunHistory.

        Parameters
        ----------
        runhistory : RunHistory
            Runhistory with additional data to be added to self
        origin : DataOrigin, defaults to DataOrigin.EXTERNAL_SAME_INSTANCES
            If set to ``INTERNAL`` or ``EXTERNAL_FULL`` the data will be
            added to the internal data structure self._config_id_to_inst_seed_budget
            and be available :meth:`through get_trials`.
        """
        # Configurations might be already known, but by a different ID. This
        # does not matter here because the add() method handles this
        # correctly by assigning an ID to unknown configurations and re-using the ID.
        for key, value in runhistory.items():
            config = runhistory._ids_config[key.config_id]
            self.add(
                config=config,
                cost=value.cost,
                time=value.time,
                status=value.status,
                instance=key.instance,
                starttime=value.starttime,
                endtime=value.endtime,
                seed=key.seed,
                budget=key.budget,
                additional_info=value.additional_info,
                origin=origin,
            )

    def update_costs(self, instances: list[str] | None = None) -> None:
        """Computes the cost of all configurations from scratch and overwrites `self._cost_per_config`
        and `self._num_trials_per_config` accordingly.

        Parameters
        ----------
        instances: list[str] | None, defaults to none
            List of instances; if given, cost is only computed wrt to this instance set.
        """
        self._cost_per_config = {}
        self._num_trials_per_config = {}
        for config, config_id in self._config_ids.items():
            # Removing duplicates while keeping the order
            inst_seed_budgets = list(dict.fromkeys(self.get_trials(config, only_max_observed_budget=True)))
            if instances is not None:
                inst_seed_budgets = list(filter(lambda x: x.instance in cast(list, instances), inst_seed_budgets))

            if inst_seed_budgets:  # can be empty if never saw any trials on instances
                self._cost_per_config[config_id] = self.average_cost(config, inst_seed_budgets)
                self._min_cost_per_config[config_id] = self.min_cost(config, inst_seed_budgets)
                self._num_trials_per_config[config_id] = len(inst_seed_budgets)

    def _check_json_serializable(
        self,
        key: str,
        obj: Any,
        trial_key: TrialKey,
        trial_value: TrialValue,
    ) -> None:
        try:
            json.dumps(obj)
        except Exception as e:
            raise ValueError(
                "Cannot add %s: %s of type %s to runhistory because it raises an error during JSON encoding, "
                "please see the error above.\ntrial_key: %s\ntrial_value %s"
                % (key, str(obj), type(obj), trial_key, trial_value)
            ) from e

    def _update_objective_bounds(self) -> None:
        """Update the objective bounds based on the data in the runhistory."""
        all_costs = []
        for run_value in self._data.values():
            costs = run_value.cost
            if run_value.status == StatusType.SUCCESS:
                if not isinstance(costs, Iterable):
                    costs = [costs]

                assert len(costs) == self._n_objectives
                all_costs.append(costs)

        all_costs = np.array(all_costs, dtype=float)  # type: ignore[assignment]

        if len(all_costs) == 0:
            self._objective_bounds = [(np.inf, -np.inf)] * self._n_objectives
            return

        min_values = np.min(all_costs, axis=0)
        max_values = np.max(all_costs, axis=0)

        self._objective_bounds = []
        for min_v, max_v in zip(min_values, max_values):
            self._objective_bounds += [(min_v, max_v)]

    def _add(self, k: TrialKey, v: TrialValue, status: StatusType, origin: DataOrigin) -> None:
        """
        Actual function to add new entry to data structures.

        Note
        ----
        This method always calls `update_cost` in the multi-objective setting.
        """
        self._data[k] = v
        self._external[k] = origin

        # Update objective bounds based on raw data
        self._update_objective_bounds()

        # Do not register the cost until the run has completed
        if (
            origin
            in (
                DataOrigin.INTERNAL,
                DataOrigin.EXTERNAL_SAME_INSTANCES,
            )
            and status != StatusType.RUNNING
        ):
            # Also add to fast data structure
            isk = InstanceSeedKey(k.instance, k.seed)
            self._config_id_to_isk_to_budget[k.config_id] = self._config_id_to_isk_to_budget.get(k.config_id, {})

            # We sanity-check whether we don't mix none and str in the instances
            for isk_ in self._config_id_to_isk_to_budget[k.config_id].keys():
                if isinstance(isk_, str) != isinstance(isk, str):
                    raise ValueError(
                        "Can not mix instances of different types. "
                        f"Wants to add {isk_.instance} but found already {isk.instance}."
                    )

            if isk not in self._config_id_to_isk_to_budget[k.config_id]:
                # Add new inst-seed-key with budget to main dict
                self._config_id_to_isk_to_budget[k.config_id][isk] = [k.budget]
            # Before it was k.budget not in isk
            elif k.budget != isk.instance and k.budget != isk.seed:
                # We have to make sure that we don't mix none and float budgets
                if isinstance(self._config_id_to_isk_to_budget[k.config_id][isk][0], float) != isinstance(
                    k.budget, float
                ):
                    raise ValueError(
                        "Can not mix budgets of different types for the same instance-seed pair. "
                        f"Wants to add {k.budget} but found already "
                        f"{self._config_id_to_isk_to_budget[k.config_id][isk][0]}."
                    )

                # Append new budget to existing inst-seed-key dict
                self._config_id_to_isk_to_budget[k.config_id][isk].append(k.budget)

            # If budget is used, then update cost instead of incremental updates
            if not self._overwrite_existing_trials and k.budget == 0:
                logger.debug(f"Incremental update cost for config {k.config_id}")
                # Assumes an average across trials as cost function aggregation, this is used for
                # algorithm configuration (incremental updates are used to save time as getting the
                # cost for > 100 instances is high)
                self.incremental_update_cost(self._ids_config[k.config_id], v.cost)
            else:
                # This happens when budget > 0 (only successive halving and hyperband so far)
                logger.debug(f"Update cost for config {k.config_id}.")
                self.update_cost(config=self._ids_config[k.config_id])

    def _cost(
        self,
        config: Configuration,
        instance_seed_budget_keys: Iterable[InstanceSeedBudgetKey] | None = None,
    ) -> list[float | list[float]]:
        """Returns a list of all costs for the given config for further calculations.
        The costs are directly taken from the runhistory data.

        Parameters
        ----------
        config : Configuration
            Configuration to calculate objective for.
        instance_seed_budget_keys : list, defaults to None
            List of tuples of instance-seeds-budget keys. If None, the runhistory is
            queried for all trials of the given configuration.

        Returns
        -------
        costs: list[list[float] | list[list[float]]]
            List of all found costs. In case of multi-objective, the list contains lists.
        """
        try:
            id_ = self._config_ids[config]
        except KeyError:  # Challenger was not running so far
            return []

        if instance_seed_budget_keys is None:
            instance_seed_budget_keys = self.get_trials(config, only_max_observed_budget=True)

        costs = []
        for key in instance_seed_budget_keys:
            k = TrialKey(
                config_id=id_,
                instance=key.instance,
                seed=key.seed,
                budget=key.budget,
            )
            costs.append(self._data[k].cost)

        return costs
