from glob import glob
from pathlib import Path

import polars as pl
import polars.selectors as cs
from skrub import MultiAggJoiner
from sklearn.base import BaseEstimator


def load_table_paths(path_to_tables: str | Path):
    """Given `path_to_tables`, load all tables in memory and return them as a list.

    Args:
        path_to_tables (str | Path): Path to the tables.
    """
    table_list = []
    # path expansion, search for tables
    for table_path in glob(path_to_tables):
        table_list.append(Path(table_path))
    return table_list


def find_unique_values(table: pl.DataFrame, columns: list[str] = None) -> dict:
    """Given a dataframe and either a list of columns or None, find the unique values
    in each column in the list or all columns, then return the list of values as a dictionary
    with {column_name: [list_of_values]}

    Args:
        table (pl.DataFrame): Table to evaluate.
        columns (list[str], optional): List of columns to evaluate. If None, consider all columns.
    """
    # select the columns of interest
    if columns is not None:
        # error checking columns
        if len(columns) == 0:
            raise ValueError("No columns provided.")
        for col in columns:
            if col not in table.columns:
                raise pl.ColumnNotFoundError
    else:
        columns = table.columns

    # find the unique values
    unique_values = dict(
        table.select(cs.by_name(columns).implode().list.unique())
        .transpose(include_header=True)
        .rows()
    )
    # return the dictionary of unique values
    return unique_values


def measure_containment_tables(
    unique_values_base: dict, unique_values_candidate: dict
) -> list:
    """Given `unique_values_base` and `unique_values_candidate`, measure the containment for each pair.

    The result will be returned as a list with format `[(col_base_table_1, col_cand_table_1, similarity), (col_base_table_1, col_cand_table_2, similarity),]`

    Args:
        unique_values_base (dict): Dictionary that contains the set of unique values for each column in the base (query) table.
        unique_values_candidate (dict): Dictionary that contains the set of unique values for each column in the candidate table.
    """
    containment_list = []
    # TODO: this should absolutely get optimized
    # for each value in unique_values_base, measure the containment for every value in unique_values_candidate
    for path, dict_cand in unique_values_candidate.items():
        for col_base, values_base in unique_values_base.items():
            for col_cand, values_cand in dict_cand.items():
                containment = measure_containment(values_base, values_cand)
                tup = (col_base, path, col_cand, containment)
                containment_list.append(tup)
    # convert the containment list to a pl dataframe and return that
    df_cont = pl.from_records(
        containment_list, ["query_column", "cand_path", "cand_column", "containment"]
    ).filter(pl.col("containment") > 0)
    return df_cont


def measure_containment(unique_values_query: set, unique_values_candidate: set):
    """Given `unique_values_query` and `unique_values_candidate`, measure the Jaccard Containment of the query in the
    candidate column. Return only the containment

    Args:
        unique_values_query (set): Set of unique values in the query.
        unique_values_candidate (set): Set of unique values in the candidate column.
    """
    # measure containment
    set_query = set(unique_values_query)
    containment = len(set_query.intersection(set(unique_values_candidate))) / len(
        set_query
    )
    # return containment
    return containment


def prepare_ranking(containment_list: list[tuple], budget: int):
    """Sort the containment list and cut all candidates past a certain budget.

    Args:
        containment_list (list[tuple]): List of candidates with format (query_column, cand_table, cand_column, similarity).
        budget (int): Number of candidates to keep from the list.
    """

    # Sort the list
    containment_list = containment_list.sort("containment", descending=True)

    # TODO: Somewhere here we might want to do some fancy filtering of the candidates in the ranking (with profiling)

    # Return `budget` candidates
    ranking = containment_list.top_k(budget, by="containment")
    return ranking.rows()


def execute_join(
    base_table: pl.DataFrame,
    candidate_list: dict[tuple],
    multiaggjoiner_params: dict | None = None,
):
    """Execute a full join between the base table and all candidates.

    Args:
        base_table (pl.DataFrame): _description_
        candidate_list (dict[pl.DataFrame]): _description_
    """

    join_tables = []
    join_keys = []
    main_keys = []
    for candidate in candidate_list:
        main_table_key, aux_table, aux_table_key, similarity = candidate
        table = pl.read_parquet(aux_table)
        join_tables.append(table)
        join_keys.append([aux_table_key])
        main_keys.append([main_table_key])

    # Use the Skrub MultiAggJoiner to join the base table and all candidates.
    if multiaggjoiner_params is None:
        multiaggjoiner_params = {}
    aggjoiner = MultiAggJoiner(
        aux_tables=join_tables,
        aux_keys=join_keys,
        main_keys=main_keys,
        **multiaggjoiner_params,
    )
    # execute join between X and the candidates
    _joined_table = aggjoiner.fit_transform(base_table)

    # Return the joined table
    return _joined_table


class Discover(BaseEstimator):
    # TODO: this should extend the sklearn BaseEstimator
    def __init__(
        self,
        path_tables: list,
        query_columns: (
            list | str
        ),  # TODO: maybe query_columns should be optional? depends on the caching
        path_cache: str | Path = None,
        budget=30,
        multiaggjoiner_params: dict | None = None,
    ) -> None:
        # Assign parameters
        self.query_columns = query_columns
        self.budget = budget
        self.path_tables = path_tables
        self.path_cache = path_cache
        self.multiaggjoiner_params = multiaggjoiner_params

        # Instantiate internal parameters
        self._ranking = None
        self._unique_values_candidates = {}
        self._candidate_paths = None

    def fit(self, X: pl.DataFrame, y=None):
        # Having more than 1 colummn is not supported.
        if len(self.query_columns) > 1:
            raise NotImplementedError
        for col in self.query_columns:
            if col not in X.columns:
                raise pl.ColumnNotFoundError(f"Column {col} not found in X.")

        # load list of tables
        self._candidate_paths = load_table_paths(self.path_tables)

        # find unique values for each table
        for table_path in self._candidate_paths:
            table = pl.read_parquet(table_path)
            self._unique_values_candidates[table_path] = find_unique_values(table)

        # find unique values for the query columns
        unique_values_X = find_unique_values(X, self.query_columns)
        # measure containment
        containment_list = measure_containment_tables(
            unique_values_X, self._unique_values_candidates
        )
        # prepare ranking
        self._ranking = prepare_ranking(containment_list, budget=self.budget)

    def transform(self, X):
        _joined = execute_join(X, self._ranking, self.multiaggjoiner_params)
        return _joined

    def fit_transform(self, X, y):
        self.fit(X, y)
        return self.transform(X)


if __name__ == "__main__":
    # working with binary to debug
    data_lake_path = "data/binary_update/*.parquet"
    base_table_path = "data/source_tables/yadl/movies_large-yadl-depleted.parquet"
    query_column = "col_to_embed"

    base_table = pl.read_parquet(base_table_path)

    discover = Discover(data_lake_path, [query_column])
    print("fitting")
    discover.fit(base_table)
    print("transforming")
    joined_table = discover.transform(base_table)
    print(joined_table)
