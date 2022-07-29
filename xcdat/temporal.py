"""Module containing temporal functions."""
from itertools import chain
from typing import Dict, List, Literal, Optional, Tuple, TypedDict, get_args

import cf_xarray  # noqa: F401
import cftime
import numpy as np
import pandas as pd
import xarray as xr
from xarray.core.groupby import DataArrayGroupBy

from xcdat import bounds  # noqa: F401
from xcdat.axis import get_axis_coord
from xcdat.dataset import _get_data_var
from xcdat.logger import setup_custom_logger

logger = setup_custom_logger(__name__)

# Type alias for supported time averaging modes.
Mode = Literal["average", "group_average", "climatology", "departures"]
#: Tuple of supported temporal averaging modes.
MODES = get_args(Mode)

# Type alias for supported grouping frequencies.
Frequency = Literal["year", "season", "month", "day", "hour"]
#: Tuple of supported grouping frequencies.
FREQUENCIES = get_args(Frequency)

# Type alias representing xarray datetime accessor components.
# https://xarray.pydata.org/en/stable/user-guide/time-series.html#datetime-components
DateTimeComponent = Literal["year", "season", "month", "day", "hour"]

#: A dictionary mapping temporal averaging mode and frequency to the time groups.
TIME_GROUPS: Dict[Mode, Dict[Frequency, Tuple[DateTimeComponent, ...]]] = {
    "average": {
        "year": ("year",),
        "month": ("month",),
        "day": ("day",),
        "hour": ("hour",),
    },
    "group_average": {
        "year": ("year",),
        "season": ("year", "season"),
        "month": ("year", "month"),
        "day": ("year", "month", "day"),
        "hour": ("year", "month", "day", "hour"),
    },
    "climatology": {
        "season": ("season",),
        "month": ("month",),
        "day": ("month", "day"),
    },
    "departures": {
        "season": ("season",),
        "month": ("month",),
        "day": ("month", "day"),
    },
}

# Configuration specific to the "season" frequency.
SeasonConfigInput = TypedDict(
    "SeasonConfigInput",
    {
        "dec_mode": Literal["DJF", "JFD"],
        "drop_incomplete_djf": bool,
        "custom_seasons": Optional[List[List[str]]],
    },
    total=False,
)

SeasonConfigAttr = TypedDict(
    "SeasonConfigAttr",
    {
        "dec_mode": Literal["DJF", "JFD"],
        "drop_incomplete_djf": bool,
        "custom_seasons": Optional[Dict[str, List[str]]],
    },
    total=False,
)

DEFAULT_SEASON_CONFIG: SeasonConfigInput = {
    "dec_mode": "DJF",
    "drop_incomplete_djf": False,
    "custom_seasons": None,
}

#: A dictionary mapping month integers to their equivalent 3-letter string.
MONTH_INT_TO_STR: Dict[int, str] = {
    1: "Jan",
    2: "Feb",
    3: "Mar",
    4: "Apr",
    5: "May",
    6: "Jun",
    7: "Jul",
    8: "Aug",
    9: "Sep",
    10: "Oct",
    11: "Nov",
    12: "Dec",
}

# A dictionary mapping pre-defined seasons to their middle month. This
# dictionary is used during the creation of datetime objects, which don't
# support season values.
SEASON_TO_MONTH: Dict[str, int] = {"DJF": 1, "MAM": 4, "JJA": 7, "SON": 10}


@xr.register_dataset_accessor("temporal")
class TemporalAccessor:
    """
    An accessor class that provides temporal attributes and methods on xarray
    Datasets through the ``.temporal`` attribute.

    Examples
    --------

    Import TemporalAccessor class:

    >>> import xcdat  # or from xcdat import temporal

    Use TemporalAccessor class:

    >>> ds = xcdat.open_dataset("/path/to/file")
    >>>
    >>> ds.temporal.<attribute>
    >>> ds.temporal.<method>
    >>> ds.temporal.<property>

    Check the 'axis' attribute is set on the time coordinates:

    >>> ds.time.attrs["axis"]
    >>> T

    Set the 'axis' attribute for the time coordinates if it isn't:

    >>> ds.time.attrs["axis"] = "T"

    Parameters
    ----------
    dataset : xr.Dataset
        A Dataset object.
    """

    def __init__(self, dataset: xr.Dataset):
        self._dataset: xr.Dataset = dataset

        self._dim = get_axis_coord(self._dataset, "T").name

        self._time_bounds = self._dataset.bounds.get_bounds("T").copy()

    def average(self, data_var: str, weighted: bool = True, keep_weights: bool = False):
        """
        Returns a Dataset with the average of a data variable and the time
        dimension removed.

        This method is particularly useful for calculating the weighted averages
        of monthly or yearly time series data because the number of days per
        month/year can vary based on the calendar type, which can affect
        weighting. For other frequencies, the distribution of weights will be
        equal so ``weighted=True`` is the same as ``weighted=False``.

        Parameters
        ----------
        data_var: str
            The key of the data variable for calculating averages

        weighted : bool, optional
            Calculate averages using weights, by default True.

            Weights are calculated by first determining the length of time for
            each coordinate point using the difference of its upper and lower
            bounds. The time lengths are grouped, then each time length is
            divided by the total sum of the time lengths to get the weight of
            each coordinate point.

            The weight of masked (missing) data is excluded when averages are
            taken. This is the same as giving them a weight of 0.
        keep_weights : bool, optional
            If calculating averages using weights, keep the weights in the
            final dataset output, by default False.

        Returns
        -------
        xr.Dataset
            Dataset with the average of the data variable and the time dimension
            removed.

        Examples
        --------

        Get weighted averages for a monthly time series data variable:

        >>> ds_month = ds.temporal.average("ts", freq="month")
        >>> ds_month.ts
        """
        freq = self._infer_freq()

        return self._averager(data_var, "average", freq, weighted, keep_weights)

    def group_average(
        self,
        data_var: str,
        freq: Frequency,
        weighted: bool = True,
        keep_weights: bool = False,
        season_config: SeasonConfigInput = DEFAULT_SEASON_CONFIG,
    ):
        """Returns a Dataset with average of a data variable by time group.

        Parameters
        ----------
        data_var: str
            The key of the data variable for calculating time series averages.
        freq : Frequency
            The time frequency to group by.

            * "year": groups by year for yearly averages.
            * "season": groups by (year, season) for seasonal averages.
            * "month": groups by (year, month) for monthly averages.
            * "day": groups by (year, month, day) for daily averages.
            * "hour": groups by (year, month, day, hour) for hourly averages.

        weighted : bool, optional
            Calculate averages using weights, by default True.

            Weights are calculated by first determining the length of time for
            each coordinate point using the difference of its upper and lower
            bounds. The time lengths are grouped, then each time length is
            divided by the total sum of the time lengths to get the weight of
            each coordinate point.

            The weight of masked (missing) data is excluded when averages are
            calculated. This is the same as giving them a weight of 0.
        keep_weights : bool, optional
            If calculating averages using weights, keep the weights in the
            final dataset output, by default False.
        season_config: SeasonConfigInput, optional
            A dictionary for "season" frequency configurations. If configs for
            predefined seasons are passed, configs for custom seasons are
            ignored and vice versa.

            Configs for predefined seasons:

            * "dec_mode" (Literal["DJF", "JFD"], by default "DJF")
                The mode for the season that includes December.

                * "DJF": season includes the previous year December.
                * "JFD": season includes the same year December.
                    Xarray labels the season with December as "DJF", but it is
                    actually "JFD".

            * "drop_incomplete_djf" (bool, by default False)
                If the "dec_mode" is "DJF", this flag drops (True) or keeps
                (False) time coordinates that fall under incomplete DJF seasons
                Incomplete DJF seasons include the start year Jan/Feb and the
                end year Dec.

            Configs for custom seasons:

            * "custom_seasons" ([List[List[str]]], by default None)
                List of sublists containing month strings, with each sublist
                representing a custom season.

                * Month strings must be in the three letter format (e.g., 'Jan')
                * Each month must be included once in a custom season
                * Order of the months in each custom season does not matter
                * Custom seasons can vary in length

                >>> # Example of custom seasons in a three month format:
                >>> custom_seasons = [
                >>>     ["Jan", "Feb", "Mar"],  # "JanFebMar"
                >>>     ["Apr", "May", "Jun"],  # "AprMayJun"
                >>>     ["Jul", "Aug", "Sep"],  # "JunJulAug"
                >>>     ["Oct", "Nov", "Dec"],  # "OctNovDec"
                >>> ]

        Returns
        -------
        xr.Dataset
            Dataset with the average of a data variable by time group.

        Examples
        --------

        Get seasonal averages for a data variable:

        >>> ds_season = ds.temporal.group_average(
        >>>     "ts",
        >>>     "season",
        >>>     season_config={
        >>>         "dec_mode": "DJF",
        >>>         "drop_incomplete_season": True
        >>>     }
        >>> )
        >>> ds_season.ts
        >>>
        >>> ds_season_with_jfd = ds.temporal.group_average(
        >>>     "ts",
        >>>     "season",
        >>>     season_config={"dec_mode": "JFD"}
        >>> )
        >>> ds_season_with_jfd.ts

        Get seasonal averages with custom seasons for a data variable:

        >>> custom_seasons = [
        >>>     ["Jan", "Feb", "Mar"],  # "JanFebMar"
        >>>     ["Apr", "May", "Jun"],  # "AprMayJun"
        >>>     ["Jul", "Aug", "Sep"],  # "JunJulAug"
        >>>     ["Oct", "Nov", "Dec"],  # "OctNovDec"
        >>> ]
        >>>
        >>> ds_season_custom = ds.temporal.group_average(
        >>>     "ts",
        >>>     "season",
        >>>     season_config={"custom_seasons": custom_seasons}
        >>> )

        Get the ``average()`` operation attributes:

        >>> ds_season_with_djf.ts.attrs
        {
            'operation': 'temporal_avg',
            'mode': 'average',
            'freq': 'season',
            'weighted': 'True',
            'dec_mode': 'DJF',
            'drop_incomplete_djf': 'False'
        }
        """
        return self._averager(
            data_var, "group_average", freq, weighted, keep_weights, season_config
        )

    def climatology(
        self,
        data_var: str,
        freq: Frequency,
        weighted: bool = True,
        keep_weights: bool = False,
        season_config: SeasonConfigInput = DEFAULT_SEASON_CONFIG,
    ):
        """Returns a Dataset with the climatology of a data variable.

        Parameters
        ----------
        data_var: str
            The key of the data variable for calculating climatology.
        freq : Frequency
            The time frequency to group by.

            * "season": groups by season for the seasonal cycle climatology.
            * "month": groups by month for the annual cycle climatology.
            * "day": groups by (month, day) for the daily cycle climatology.

        weighted : bool, optional
            Calculate averages using weights, by default True.

            Weights are calculated by first determining the length of time for
            each coordinate point using the difference of its upper and lower
            bounds. The time lengths are grouped, then each time length is
            divided by the total sum of the time lengths to get the weight of
            each coordinate point.

            The weight of masked (missing) data is excluded when averages are
            taken. This is the same as giving them a weight of 0.
        keep_weights : bool, optional
            If calculating averages using weights, keep the weights in the
            final dataset output, by default False.
        season_config: SeasonConfigInput, optional
            A dictionary for "season" frequency configurations. If configs for
            predefined seasons are passed, configs for custom seasons are
            ignored and vice versa.

            Configs for predefined seasons:

            * "dec_mode" (Literal["DJF", "JFD"], by default "DJF")
               The mode for the season that includes December.

                * "DJF": season includes the previous year December.
                * "JFD": season includes the same year December.
                    Xarray labels the season with December as "DJF", but it is
                    actually "JFD".

            * "drop_incomplete_djf" (bool, by default False)
                If the "dec_mode" is "DJF", this flag drops (True) or keeps
                (False) time coordinates that fall under incomplete DJF seasons
                Incomplete DJF seasons include the start year Jan/Feb and the
                end year Dec.

            Configs for custom seasons:

            * "custom_seasons" ([List[List[str]]], by default None)
                List of sublists containing month strings, with each sublist
                representing a custom season.

                * Month strings must be in the three letter format (e.g., 'Jan')
                * Each month must be included once in a custom season
                * Order of the months in each custom season does not matter
                * Custom seasons can vary in length

                >>> # Example of custom seasons in a three month format:
                >>> custom_seasons = [
                >>>     ["Jan", "Feb", "Mar"],  # "JanFebMar"
                >>>     ["Apr", "May", "Jun"],  # "AprMayJun"
                >>>     ["Jul", "Aug", "Sep"],  # "JunJulAug"
                >>>     ["Oct", "Nov", "Dec"],  # "OctNovDec"
                >>> ]

        Returns
        -------
        xr.Dataset
            Dataset with the climatology of a data variable.

        Examples
        --------

        Get a data variable's seasonal climatology:

        >>> ds_season = ds.temporal.climatology(
        >>>     "ts",
        >>>     "season",
        >>>     season_config={
        >>>         "dec_mode": "DJF",
        >>>         "drop_incomplete_season": True
        >>>     }
        >>> )
        >>> ds_season.ts
        >>>
        >>> ds_season = ds.temporal.climatology(
        >>>     "ts",
        >>>     "season",
        >>>     season_config={"dec_mode": "JFD"}
        >>> )
        >>> ds_season.ts

        Get a data variable's seasonal climatology with custom seasons:

        >>> custom_seasons = [
        >>>     ["Jan", "Feb", "Mar"],  # "JanFebMar"
        >>>     ["Apr", "May", "Jun"],  # "AprMayJun"
        >>>     ["Jul", "Aug", "Sep"],  # "JunJulAug"
        >>>     ["Oct", "Nov", "Dec"],  # "OctNovDec"
        >>> ]
        >>>
        >>> ds_season_custom = ds.temporal.climatology(
        >>>     "ts",
        >>>     "season",
        >>>     season_config={"custom_seasons": custom_seasons}
        >>> )

        Get ``climatology()`` operation attributes:

        >>> ds_season_with_djf.ts.attrs
        {
            'operation': 'temporal_avg',
            'mode': 'climatology',
            'freq': 'season',
            'weighted': 'True',
            'dec_mode': 'DJF',
            'drop_incomplete_djf': 'False'
        }
        """
        return self._averager(
            data_var, "climatology", freq, weighted, keep_weights, season_config
        )

    def departures(
        self,
        data_var: str,
        freq: Frequency,
        weighted: bool = True,
        keep_weights: bool = False,
        season_config: SeasonConfigInput = DEFAULT_SEASON_CONFIG,
    ) -> xr.Dataset:
        """
        Returns a Dataset with the climatological departures (anomalies) for a
        data variable.

        In climatology, “anomalies” refer to the difference between the value
        during a given time interval (e.g., the January average surface air
        temperature) and the long-term average value for that time interval
        (e.g., the average surface temperature over the last 30 Januaries).

        This method uses xarray's grouped arithmetic as a shortcut for mapping
        over all unique labels. Grouped arithmetic works by assigning a grouping
        label to each time coordinate of the observation data based on the
        averaging mode and frequency. Afterwards, the corresponding climatology
        is removed from the observation data at each time coordinate based on
        the matching labels.

        xarray's grouped arithmetic operates over each value of the DataArray
        corresponding to each grouping label without changing the size of the
        DataArray. For example,the original monthly time coordinates are
        maintained when calculating seasonal departures on monthly data.

        Parameters
        ----------
        data_var: str
            The key of the data variable for calculating departures.

        freq : Frequency
            The frequency of time to group by.

            * "season": groups by season for the seasonal cycle departures.
            * "month": groups by month for the annual cycle departures.
            * "day": groups by (month, day) for the daily cycle departures.

        weighted : bool, optional
            Calculate averages using weights, by default True.

            Weights are calculated by first determining the length of time for
            each coordinate point using the difference of its upper and lower
            bounds. The time lengths are grouped, then each time length is
            divided by the total sum of the time lengths to get the weight of
            each coordinate point.

            The weight of masked (missing) data is excluded when averages are
            taken. This is the same as giving them a weight of 0.
        keep_weights : bool, optional
            If calculating averages using weights, keep the weights in the
            final dataset output, by default False.
        season_config: SeasonConfigInput, optional
            A dictionary for "season" frequency configurations. If configs for
            predefined seasons are passed, configs for custom seasons are
            ignored and vice versa.

            Configs for predefined seasons:

            * "dec_mode" (Literal["DJF", "JFD"], by default "DJF")
               The mode for the season that includes December.

                * "DJF": season includes the previous year December.
                * "JFD": season includes the same year December.
                    Xarray labels the season with December as "DJF", but it is
                    actually "JFD".

            * "drop_incomplete_djf" (bool, by default False)
                If the "dec_mode" is "DJF", this flag drops (True) or keeps
                (False) time coordinates that fall under incomplete DJF seasons
                Incomplete DJF seasons include the start year Jan/Feb and the
                end year Dec.

            Configs for custom seasons:

            * "custom_seasons" ([List[List[str]]], by default None)
                List of sublists containing month strings, with each sublist
                representing a custom season.

                * Month strings must be in the three letter format (e.g., 'Jan')
                * Each month must be included once in a custom season
                * Order of the months in each custom season does not matter
                * Custom seasons can vary in length

                >>> # Example of custom seasons in a three month format:
                >>> custom_seasons = [
                >>>     ["Jan", "Feb", "Mar"],  # "JanFebMar"
                >>>     ["Apr", "May", "Jun"],  # "AprMayJun"
                >>>     ["Jul", "Aug", "Sep"],  # "JunJulAug"
                >>>     ["Oct", "Nov", "Dec"],  # "OctNovDec"
                >>> ]

        Returns
        -------
        xr.Dataset
            The Dataset containing the departures for a data var's climatology.

        Notes
        -----
        Refer to [1]_ to learn more about how xarray's grouped arithmetic works.

        References
        ----------
        .. [1] https://xarray.pydata.org/en/stable/user-guide/groupby.html#grouped-arithmetic

        Examples
        --------

        Get a data variable's annual cycle departures:

        >>> ds_depart = ds_climo.temporal.departures("ts", "month")

        Get the ``departures()`` operation attributes:

        >>> ds_depart.ts.attrs
        {
            'operation': 'departures',
            'frequency': 'season',
            'weighted': 'True',
            'dec_mode': 'DJF',
            'drop_incomplete_djf': 'False'
        }
        """
        ds = self._dataset.copy()
        self._set_obj_attrs("departures", freq, weighted, season_config)

        # Group the observation data variable.
        dv_obs = _get_data_var(ds, data_var)
        dv_obs_grouped = self._group_data(dv_obs)

        # Calculate the climatology of the data variable.
        ds_climo = ds.temporal.climatology(
            data_var, freq, weighted, keep_weights, season_config
        )
        dv_climo = ds_climo[data_var]

        # Rename the time dimension using the name from the grouped observation
        # data. The dimension names must align for xarray's grouped arithmetic
        # to work. Otherwise, the error below is thrown: `ValueError:
        # incompatible dimensions for a grouped binary operation: the group
        # variable '<CHOSEN_FREQ>' is not a dimension on the other argument`
        dv_climo = dv_climo.rename({self._dim: self._labeled_time.name})

        # Use xarray's grouped arithmetic to calculate the departures, which is
        # the difference grouped observation data variable and its climatology.
        with xr.set_options(keep_attrs=True):
            ds_departs = self._dataset.copy()
            ds_departs[data_var] = dv_obs_grouped - dv_climo
            ds_departs[data_var] = self._add_operation_attrs(ds_departs[data_var])

            # The original time coordinates are restored after grouped
            # subtraction. The grouped time coordinates are dropped since they
            # are no longer required.
            ds_departs = ds_departs.drop_vars(self._labeled_time.name)

        if weighted and keep_weights:
            self._weights = ds_climo.time_wts
            ds_departs = self._keep_weights(ds_departs)

        return ds_departs

    def _infer_freq(self) -> Frequency:
        """Infers the time frequency from the coordinates.

        This method infers the time frequency from the coordinates by
        calculating the minimum delta and comparing it against a set of
        conditionals.

        The native ``xr.infer_freq()`` method does not work for all cases
        because the frequency can be irregular (e.g., different hour
        measurements), which ends up returning None.

        Returns
        -------
        Frequency
            The time frequency.
        """
        time_coords = self._dataset[self._dim]
        min_delta = pd.to_timedelta(np.diff(time_coords).min(), unit="ns")

        if min_delta < pd.Timedelta(days=1):
            return "hour"
        elif min_delta >= pd.Timedelta(days=1) and min_delta < pd.Timedelta(days=28):
            return "day"
        elif min_delta >= pd.Timedelta(days=28) and min_delta < pd.Timedelta(days=365):
            return "month"
        else:
            return "year"

    def _averager(
        self,
        data_var: str,
        mode: Mode,
        freq: Frequency,
        weighted: bool = True,
        keep_weights: bool = False,
        season_config: SeasonConfigInput = DEFAULT_SEASON_CONFIG,
    ) -> xr.Dataset:
        """Averages a data variable based on the averaging mode and frequency."""
        ds = self._dataset.copy()
        self._set_obj_attrs(mode, freq, weighted, season_config)

        if (
            self._freq == "season"
            and self._season_config.get("dec_mode") == "DJF"
            and self._season_config.get("drop_incomplete_djf") is True
        ):
            ds = self._drop_incomplete_djf(ds)

        dv = _get_data_var(ds, data_var)
        if self._mode == "average":
            dv = self._average(dv)
        elif self._mode in ["group_average", "climatology", "departures"]:
            dv = self._group_average(dv)

        # The original time dimension is dropped from the dataset because
        # it becomes obsolete after the data variable is averaged. When the
        # averaged data variable is added to the dataset, the new time dimension
        # and its associated coordinates are also added.
        ds = ds.drop_dims("time")
        ds[dv.name] = dv

        if keep_weights:
            ds = self._keep_weights(ds)

        return ds

    def _set_obj_attrs(
        self,
        mode: Mode,
        freq: Frequency,
        weighted: bool,
        season_config: SeasonConfigInput = DEFAULT_SEASON_CONFIG,
    ):
        """Validates method arguments and sets them as object attributes.

        Parameters
        ----------
        mode : Mode
            The mode for temporal averaging.
        freq : Frequency
            The frequency of time to group by.
        weighted : bool
            Calculate averages using weights.
        season_config: Optional[SeasonConfigInput]
            A dictionary for "season" frequency configurations. If configs for
            predefined seasons are passed, configs for custom seasons are
            ignored and vice versa, by default DEFAULT_SEASON_CONFIG.

        Raises
        ------
        KeyError
            If the Dataset does not have a "time" dimension.
        ValueError
            If an incorrect ``freq`` arg was passed.
        ValueError
            If an incorrect ``dec_mode`` arg was passed.
        """
        # General configuration attributes.
        if mode not in list(MODES):
            modes = ", ".join(f'"{word}"' for word in MODES)
            raise ValueError(
                f"Incorrect `mode` argument. Supported modes include: " f"{modes}."
            )

        freq_keys = TIME_GROUPS[mode].keys()
        if freq not in freq_keys and "hour" not in freq:
            raise ValueError(
                f"Incorrect `freq` argument. Supported frequencies for {mode} "
                f"include: {list(freq_keys)}."
            )

        self._mode = mode
        self._freq = freq
        self._weighted = weighted

        # "season" frequency specific configuration attributes.
        for key in season_config.keys():
            if key not in DEFAULT_SEASON_CONFIG.keys():
                raise KeyError(
                    f"'{key}' is not a supported season config. Supported "
                    f"configs include: {DEFAULT_SEASON_CONFIG.keys()}."
                )
        custom_seasons = season_config.get("custom_seasons", None)
        dec_mode = season_config.get("dec_mode", "DJF")
        drop_incomplete_djf = season_config.get("drop_incomplete_djf", False)

        self._season_config: SeasonConfigAttr = {}
        if custom_seasons is None:
            if dec_mode not in ("DJF", "JFD"):
                raise ValueError(
                    "Incorrect 'dec_mode' key value for `season_config`. "
                    "Supported modes include 'DJF' or 'JFD'."
                )
            self._season_config["dec_mode"] = dec_mode

            if dec_mode == "DJF":
                self._season_config["drop_incomplete_djf"] = drop_incomplete_djf
        else:
            self._season_config["custom_seasons"] = self._form_seasons(custom_seasons)

    def _drop_incomplete_djf(self, dataset: xr.Dataset) -> xr.Dataset:
        """Drops incomplete DJF seasons within a continuous time series.

        This method assumes that the time series is continuous and removes the
        leading and trailing incomplete seasons (e.g., the first January and
        February of a time series that are not complete, because the December of
        the previous year is missing). This method does not account for or
        remove missing time steps anywhere else.

        Parameters
        ----------
        data_var : xr.DataArray
            The data variable with some possibly incomplete DJF seasons.

        Returns
        -------
        xr.DataArray
            The data variable with only complete DJF seasons.
        """
        # Separate the dataset into two datasets, one with and one without
        # the time dimension. This is necessary because the xarray .where()
        # method concatenates the time dimension to non-time dimension data
        # vars, which is not a desired behavior.
        ds = dataset.copy()
        ds_time = ds.get([v for v in ds.data_vars if "time" in ds[v].dims])
        ds_no_time = ds.get([v for v in ds.data_vars if "time" not in ds[v].dims])

        start_year, end_year = (ds.time.dt.year.values[0], ds.time.dt.year.values[-1])
        incomplete_seasons = (f"{start_year}-01", f"{start_year}-02", f"{end_year}-12")
        for year_month in incomplete_seasons:
            try:
                coord_pt = ds.loc[dict(time=year_month)].time[0]
                ds_time = ds_time.where(ds_time.time != coord_pt, drop=True)  # type: ignore
                self._time_bounds = ds_time[self._time_bounds.name]
            except (KeyError, IndexError):
                continue

        ds_final = xr.merge((ds_time, ds_no_time))  # type: ignore

        return ds_final

    def _form_seasons(self, custom_seasons: List[List[str]]) -> Dict[str, List[str]]:
        """Forms custom seasons from a nested list of months.

        This method concatenates the strings in each sublist to form a
        a flat list of custom season strings

        Parameters
        ----------
        custom_seasons : List[List[str]]
            List of sublists containing month strings, with each sublist
            representing a custom season.

        Returns
        -------
        Dict[str, List[str]]
           A dictionary with the keys being the custom season and the
           values being the corresponding list of months.

        Raises
        ------
        ValueError
            If exactly 12 months are not passed in the list of custom seasons.
        ValueError
            If a duplicate month(s) were found in the list of custom seasons.
        ValueError
            If a month string(s) is not supported.
        """
        predefined_months = list(MONTH_INT_TO_STR.values())
        input_months = list(chain.from_iterable(custom_seasons))

        if len(input_months) != len(predefined_months):
            raise ValueError(
                "Exactly 12 months were not passed in the list of custom seasons."
            )
        if len(input_months) != len(set(input_months)):
            raise ValueError(
                "Duplicate month(s) were found in the list of custom seasons."
            )

        for month in input_months:
            if month not in predefined_months:
                raise ValueError(
                    f"The following month is not supported: '{month}'. "
                    f"Supported months include: {predefined_months}."
                )

        c_seasons = {"".join(months): months for months in custom_seasons}

        return c_seasons

    def _average(self, data_var: xr.DataArray) -> xr.DataArray:
        """Averages a data variable with the time dimension removed.

        Parameters
        ----------
        data_var : xr.DataArray
            The data variable.

        Returns
        -------
        xr.DataArray
            The averages for a data variable with the time dimension removed.
        """
        dv = data_var.copy()

        with xr.set_options(keep_attrs=True):
            if self._weighted:
                self._weights = self._get_weights()
                dv = dv.weighted(self._weights).mean(dim=self._dim)
            else:
                dv = dv.mean(dim=self._dim)

        dv = self._add_operation_attrs(dv)

        return dv

    def _group_average(self, data_var: xr.DataArray) -> xr.DataArray:
        """Averages a data variable by time group.

        Parameters
        ----------
        data_var : xr.DataArray
            The data variable.

        Returns
        -------
        xr.DataArray
            The data variable averaged by time group.
        """
        dv = data_var.copy()

        if self._weighted:
            self._weights = self._get_weights()
            dv *= self._weights
            dv = self._group_data(dv).sum()  # type: ignore
        else:
            dv = self._group_data(dv).mean()  # type: ignore

        # After grouping and aggregating the data variable values, the
        # original time dimension is replaced with the grouped time dimension.
        # For example, grouping on "year_season" replaces the "time" dimension
        # with "year_season". This dimension needs to be renamed back to
        # the original time dimension name before the data variable is added
        # back to the dataset so that the CF compliant name is maintained.
        dv = dv.rename({self._labeled_time.name: self._dim})  # type: ignore

        # After grouping and aggregating, the grouped time dimension's
        # attributes are removed. Xarray's `keep_attrs=True` option only keeps
        # attributes for data variables and not their coordinates, so the
        # coordinate attributes have to be restored manually.
        dv[self._dim].attrs = self._labeled_time.attrs
        dv[self._dim].encoding = self._labeled_time.encoding

        dv = self._add_operation_attrs(dv)

        return dv

    def _get_weights(self) -> xr.DataArray:
        """Calculates weights for a data variable using time bounds.

        This method gets the length of time for each coordinate point by using
        the difference in the upper and lower time bounds. This approach ensures
        that the correct time lengths are calculated regardless of how time
        coordinates are recorded (e.g., monthly, daily, hourly) and the calendar
        type used.

        The time lengths are labeled and grouped, then each time length is
        divided by the total sum of the time lengths in its group to get its
        corresponding weight.

        The sum of the weights for each group is validated to ensure it equals
        1.0.

        Returns
        -------
        xr.DataArray
            The weights based on a specified frequency.

        Notes
        -----
        Refer to [5]_ for the supported CF convention calendar types.

        References
        ----------
        .. [5] https://cfconventions.org/cf-conventions/cf-conventions.html#calendar
        """
        with xr.set_options(keep_attrs=True):
            time_lengths: xr.DataArray = (
                self._time_bounds[:, 1] - self._time_bounds[:, 0]
            )

        # Must be cast dtype from "timedelta64[ns]" to "float64", specifically
        # when using Dask arrays. Otherwise, the numpy warning below is thrown:
        # `DeprecationWarning: The `dtype` and `signature` arguments to ufuncs
        # only select the general DType and not details such as the byte order
        # or time unit (with rare exceptions see release notes). To avoid this
        # warning please use the scalar types `np.float64`, or string notation.`
        time_lengths.load()
        time_lengths = time_lengths.astype(np.float64)

        grouped_time_lengths = self._group_data(time_lengths)
        weights: xr.DataArray = grouped_time_lengths / grouped_time_lengths.sum()  # type: ignore
        weights.name = f"{self._dim}_wts"

        # Validate the sum of weights for each group is 1.0.
        actual_sum = self._group_data(weights).sum().values  # type: ignore
        expected_sum = np.ones(len(grouped_time_lengths.groups))
        np.testing.assert_allclose(actual_sum, expected_sum)

        return weights

    def _group_data(self, data_var: xr.DataArray) -> DataArrayGroupBy:
        """Groups a data variable.

        This method groups a data variable by a single datetime component for
        the "average" mode or labeled time coordinates for all other modes.

        Parameters
        ----------
        data_var : xr.DataArray
            A data variable.

        Returns
        -------
        DataArrayGroupBy
            A data variable grouped by label.
        """
        dv = data_var.copy()

        if self._mode == "average":
            dv_gb = dv.groupby(f"{self._dim}.{self._freq}")
        else:
            self._labeled_time = self._label_time_coords(dv[self._dim])
            dv.coords[self._labeled_time.name] = self._labeled_time
            dv_gb = dv.groupby(self._labeled_time.name)

        return dv_gb

    def _label_time_coords(self, time_coords: xr.DataArray) -> xr.DataArray:
        """Labels time coordinates with a group for grouping.

        This methods labels time coordinates for grouping by first extracting
        specific xarray datetime components from time coordinates and storing
        them in a pandas DataFrame. After processing (if necessary) is performed
        on the DataFrame, it is converted to a numpy array of datetime
        objects. This numpy serves as the data source for the final
        DataArray of labeled time coordinates.

        Parameters
        ----------
        time_coords : xr.DataArray
            The time coordinates.

        Returns
        -------
        xr.DataArray
            The DataArray of labeled time coordinates for grouping.

        Examples
        --------

        Original daily time coordinates:

        >>> <xarray.DataArray 'time' (time: 4)>
        >>> array(['2000-01-01T12:00:00.000000000',
        >>>        '2000-01-31T21:00:00.000000000',
        >>>        '2000-03-01T21:00:00.000000000',
        >>>        '2000-04-01T03:00:00.000000000'],
        >>>       dtype='datetime64[ns]')
        >>> Coordinates:
        >>> * time     (time) datetime64[ns] 2000-01-01T12:00:00 ... 2000-04-01T03:00:00

        Daily time coordinates labeled by year and month:

        >>> <xarray.DataArray 'time' (time: 3)>
        >>> array(['2000-01-01T00:00:00.000000000',
        >>>        '2000-03-01T00:00:00.000000000',
        >>>        '2000-04-01T00:00:00.000000000'],
        >>>       dtype='datetime64[ns]')
        >>> Coordinates:
        >>> * time     (time) datetime64[ns] 2000-01-01T00:00:00 ... 2000-04-01T00:00:00
        """
        df_dt_components: pd.DataFrame = self._get_df_dt_components(time_coords)
        dt_objects = self._convert_df_to_dt(df_dt_components)

        time_grouped = xr.DataArray(
            name="_".join(df_dt_components.columns),
            data=dt_objects,
            coords={"time": time_coords.time},
            dims=["time"],
            attrs=time_coords.time.attrs,
        )
        time_grouped.encoding = time_coords.time.encoding

        return time_grouped

    def _get_df_dt_components(self, time_coords: xr.DataArray) -> pd.DataFrame:
        """Returns a DataFrame of xarray datetime components.

        This method extracts the applicable xarray datetime components from each
        time coordinate based on the averaging mode and frequency, and stores
        them in a DataFrame.

        Additional processing is performed for the seasonal frequency,
        including:

        * If custom seasons are used, map them to each time coordinate based
          on the middle month of the custom season.
        * If season with December is "DJF", shift Decembers over to the next
          year so DJF seasons are correctly grouped using the previous year
          December.
        * Drop obsolete columns after processing is done.

        Parameters
        ----------
        time_coords : xr.DataArray
            The time coordinates.

        Returns
        -------
        pd.DataFrame
            A DataFrame of datetime components.

        Notes
        -----
        Refer to [2]_ for information on xarray datetime accessor components.

        References
        ----------
        .. [2] https://xarray.pydata.org/en/stable/user-guide/time-series.html#datetime-components
        """
        df = pd.DataFrame()

        # Use the TIME_GROUPS dictionary to determine which components
        # are needed to form the labeled time coordinates.
        for component in TIME_GROUPS[self._mode][self._freq]:
            df[component] = time_coords[f"{self._dim}.{component}"].values

        # The season frequency requires additional datetime components for
        # processing, which are later removed before time coordinates are
        # labeled for grouping. These components weren't included in the
        # `TIME_GROUPS` dictionary for the "season" frequency because
        # `TIME_GROUPS` represents the final grouping labels.
        if self._freq == "season":
            if self._mode in ["climatology", "departures"]:
                df["year"] = time_coords[f"{self._dim}.year"].values
                df["month"] = time_coords[f"{self._dim}.month"].values

            if self._mode == "group_average":
                df["month"] = time_coords[f"{self._dim}.month"].values

            df = self._process_season_df(df)

        return df

    def _process_season_df(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Processes a DataFrame of datetime components for the season frequency.

        Parameters
        ----------
        df : pd.DataFrame
            A DataFrame of xarray datetime components.

        Returns
        -------
        pd.DataFrame
            A DataFrame of processed xarray datetime components.
        """
        df_new = df.copy()
        custom_seasons = self._season_config.get("custom_seasons")
        dec_mode = self._season_config.get("dec_mode")

        if custom_seasons is not None:
            df_new = self._map_months_to_custom_seasons(df_new)
        else:
            if dec_mode == "DJF":
                df_new = self._shift_decembers(df_new)

        df_new = self._drop_obsolete_columns(df_new)
        df_new = self._map_seasons_to_mid_months(df_new)
        return df_new

    def _map_months_to_custom_seasons(self, df: pd.DataFrame) -> pd.DataFrame:
        """Maps the month column in the DataFrame to a custom season.

        This method maps each integer value in the "month" column to its string
        represention, which then maps to a custom season that is stored in the
        "season" column. For example, the month of 1 maps to "Jan" and "Jan"
        maps to the "JanFebMar" custom season.

        Parameters
        ----------
        df : pd.DataFrame
            The DataFrame of xarray datetime components.

        Returns
        -------
        pd.DataFrame
            The DataFrame of xarray datetime coordinates, with each row mapped
            to a custom season.
        """
        custom_seasons = self._season_config["custom_seasons"]

        # NOTE: This for loop has a time complexity of O(n^2), but it is fine
        # because these data structures are small.
        seasons_map = {}
        for mon_int, mon_str in MONTH_INT_TO_STR.items():
            for season in custom_seasons:  # type: ignore
                if mon_str in season:
                    seasons_map[mon_int] = season

        df_new = df.copy()
        df_new["season"] = df_new["month"].map(seasons_map)

        return df_new

    def _shift_decembers(self, df_season: pd.DataFrame) -> pd.DataFrame:
        """Shifts Decembers over to the next year for "DJF" seasons in-place.

        For "DJF" seasons, Decembers must be shifted over to the next year in
        order for the xarray groupby operation to correctly label and group the
        corresponding time coordinates. If the aren't shifted over, grouping is
        incorrectly performed with the native xarray "DJF" season (which is
        actually "JFD").

        Parameters
        ----------
        df_season : pd.DataFrame
            The DataFrame of xarray datetime components produced using the
            "season" frequency.

        Returns
        -------
        pd.DataFrame
            The DataFrame of xarray datetime components with Decembers shifted
            over to the next year.

        Examples
        --------

        Comparison of "JFD" and "DJF" seasons:

        >>> # "JFD" (native xarray behavior)
        >>> [(2000, "DJF", 1), (2000, "DJF", 2), (2000, "DJF", 12),
        >>>  (2001, "DJF", 1), (2001, "DJF", 2)]

        >>> # "DJF" (shifted Decembers)
        >>> [(2000, "DJF", 1), (2000, "DJF", 2), (2001, "DJF", 12),
        >>>  (2001, "DJF", 1), (2001, "DJF", 2)]

        """
        df_season.loc[df_season["month"] == 12, "year"] = df_season["year"] + 1

        return df_season

    def _map_seasons_to_mid_months(self, df: pd.DataFrame) -> pd.DataFrame:
        """Maps the season column values to the integer of its middle month.

        DateTime objects don't support storing seasons as strings, so the middle
        months are used to represent the season. For example, for the season
        "DJF", the middle month "J" is mapped to the integer value 1.

        The middle month of a custom season is extracted using the ceiling of
        the middle index from its list of months. For example, for the custom
        season "FebMarAprMay" with the list of months ["Feb", "Mar", "Apr",
        "May"], the index 3 is used to get the month "Apr". "Apr" is then mapped
        to the integer value 4.

        After mapping the season to its month, the "season" column is renamed to
        "month".

        Parameters
        ----------
        df : pd.DataFrame
            The dataframe of datetime components, including a "season" column.

        Returns
        -------
        pd.DataFrame
            The dataframe of datetime components, including a "month" column.
        """
        df_new = df.copy()
        custom_seasons = self._season_config.get("custom_seasons")

        if custom_seasons is None:
            season_to_month = SEASON_TO_MONTH
        else:
            season_to_month = {}

            for season, months in custom_seasons.items():
                middle_str = months[len(months) // 2]
                middle_int = [
                    k for k, v in MONTH_INT_TO_STR.items() if v == middle_str
                ][0]
                season_to_month[season] = middle_int

        df_new = df_new.rename(columns={"season": "month"})
        df_new["month"] = df_new.month.map(season_to_month)

        return df_new

    def _drop_obsolete_columns(self, df_season: pd.DataFrame) -> pd.DataFrame:
        """
        Drops obsolete columns from the DataFrame of xarray datetime components.

        For the "season" frequency, processing is required on the DataFrame of
        xarray datetime components, such as mapping custom seasons based on the
        month. Additional datetime component values must be included as
        DataFrame columns, which become obsolete after processing is done. The
        obsolete columns are dropped from the DataFrame before grouping
        time coordinates.

        Parameters
        ----------
        df_season : pd.DataFrame
            The DataFrame of time coordinates for the "season" frequency with
            obsolete columns.

        Returns
        -------
        pd.DataFrame
            The DataFrame of time coordinates for the "season" frequency with
            obsolete columns dropped.
        """
        if self._mode == "group_average":
            df_season = df_season.drop("month", axis=1)
        elif self._mode in ["climatology", "departures"]:
            df_season = df_season.drop(["year", "month"], axis=1)

        return df_season

    def _convert_df_to_dt(self, df: pd.DataFrame) -> np.ndarray:
        """Converts a DataFrame of datetime components to datetime objects.

        datetime objects require at least a year, month, and day value. However,
        some modes and time frequencies don't require year, month, and/or day
        for grouping. For these cases, use default values of 1 in order to
        meet this datetime requirement.

        If the default value of 1 is used for the years, datetime objects
        must be created using `cftime.datetime` because year 1 is outside the
        Timestamp-valid range.

        Parameters
        ----------
        df : pd.DataFrame
            The DataFrame of xarray datetime components.

        Returns
        -------
        np.ndarray
            A numpy ndarray of datetime.datetime or cftime.datetime objects.

        Notes
        -----
        Refer to [3]_ and [4]_ for more information on Timestamp-valid range.

        References
        ----------
        .. [3] https://docs.xarray.dev/en/stable/user-guide/weather-climate.html#non-standard-calendars-and-dates-outside-the-timestamp-valid-range

        .. [4] https://pandas.pydata.org/pandas-docs/stable/user_guide/timeseries.html#timestamp-limitations
        """
        df_new = df.copy()

        dt_components_defaults = {"year": 1, "month": 1, "day": 1, "hour": 0}
        for component, default_val in dt_components_defaults.items():
            if component not in df_new.columns:
                df_new[component] = default_val

        year_is_unused = self._mode in ["climatology", "departures"] or (
            self._mode == "average" and self._freq != "year"
        )
        if year_is_unused:
            dates = [
                cftime.datetime(year, month, day, hour)
                for year, month, day, hour in zip(
                    df_new.year, df_new.month, df_new.day, df_new.hour
                )
            ]
        else:
            dates = pd.to_datetime(df_new)

        return np.array(dates)

    def _keep_weights(self, ds: xr.Dataset) -> xr.Dataset:
        """Keep the weights in the dataset.

        Parameters
        ----------
        ds : xr.Dataset
            The dataset.

        Returns
        -------
        xr.Dataset
            The dataset with the weights used for averaging.
        """
        # Append "_original" to the name of the weights` time coordinates to
        # avoid conflict with the grouped time coordinates in the Dataset (can
        # have a different shape).
        if self._mode in ["group_average", "climatology"]:
            self._weights = self._weights.rename({self._dim: f"{self._dim}_original"})
            # Only keep the original time coordinates, not the ones labeled
            # by group.
            self._weights = self._weights.drop_vars(self._labeled_time.name)
        # Strip "_original" from the name of the weights` time coordinates
        # because the final departures Dataset has the original time coordinates
        # restored after performing grouped subtraction.
        elif self._mode == "departures":
            self._weights = self._weights.rename({f"{self._dim}_original": self._dim})

        ds[self._weights.name] = self._weights

        return ds

    def _add_operation_attrs(self, data_var: xr.DataArray) -> xr.DataArray:
        """Adds attributes to the data variable describing the operation.
        These attributes distinguish a data variable that has been operated on
        from its original state. The attributes in netCDF4 files do not support
        booleans or nested dictionaries, so booleans are converted to strings
        and nested dictionaries are unpacked.

        Parameters
        ----------
        data_var : xr.DataArray
            The data variable.

        Returns
        -------
        xr.DataArray
            The data variable with a temporal averaging attributes.
        """
        data_var.attrs.update(
            {
                "operation": "temporal_avg",
                "mode": self._mode,
                "freq": self._freq,
                "weighted": str(self._weighted),
            }
        )

        if self._freq == "season":
            custom_seasons = self._season_config.get("custom_seasons")

            if custom_seasons is None:
                dec_mode = self._season_config.get("dec_mode")
                drop_incomplete_djf = self._season_config.get("drop_incomplete_djf")

                data_var.attrs["dec_mode"] = dec_mode
                if dec_mode == "DJF":
                    data_var.attrs["drop_incomplete_djf"] = str(drop_incomplete_djf)
            else:
                data_var.attrs["custom_seasons"] = list(custom_seasons.keys())

        return data_var
