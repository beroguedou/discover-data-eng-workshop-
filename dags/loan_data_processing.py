import os
import pandas as pd
import pandasql as ps
from airflow import DAG
from airflow.operators.postgres_operator import PostgresOperator
from airflow.operators.dummy import DummyOperator
from airflow.operators.python import PythonOperator, BranchPythonOperator
from airflow.operators.bash import BashOperator
from airflow.utils.trigger_rule import TriggerRule
from airflow.providers.amazon.aws.hooks.s3 import S3Hook

from datetime import datetime


def _is_data_local(ti, datapath):
    is_local = os.path.exists(datapath)
    ti.xcom_push(key="is_local", value=is_local)


# Change these to your identifiers, if needed.
AWS_S3_CONN_ID = "aws_default"
SOURCE_S3_KEY = "data/raw-data/loan_data_small.csv"
SOURCE_S3_BUCKET = "beranger-bucket-760254833251"
DEST_PATH_FILE = "/opt/airflow/data/raw-data"


def _extract_from_s3(key: str, bucket_name: str, local_path: str) -> str:
    source_s3 = S3Hook(AWS_S3_CONN_ID)
    file_name = source_s3.download_file(
        key,
        bucket_name,
        local_path,
        preserve_file_name=True,
        use_autogenerated_subdir=False,
    )
    return file_name


def _branch(ti):
    value = ti.xcom_pull(key="is_local", task_ids="is_data_local")
    if value is True:
        return "log_data_is_local"
    else:
        return "log_data_is_remote"


def _compute_general_aggregats(ti, datapath: str) -> None:
    df = pd.read_csv(datapath)
    nb_lines = df.shape[0]
    nb_cols = df.shape[1]

    aggr_dict = {"nb_lines": nb_lines, "nb_cols": nb_cols}
    ti.xcom_push(key="nb_lines", value=nb_lines)
    ti.xcom_push(key="nb_cols", value=nb_cols)


def _compute_loan_aggregats(ti, datapath: str) -> None:
    df = pd.read_csv(datapath)
    query = """
            SELECT
                (   
                    SELECT AVG(loan_amount)
                    FROM df
                    WHERE grade = 'A'
                )  AS mean_loan_a,
                (   
                    SELECT AVG(loan_amount)
                    FROM df
                    WHERE grade = 'B'
                )  AS mean_loan_b,
                (   
                    SELECT AVG(loan_amount)
                    FROM df
                    WHERE grade = 'C'
                )  AS mean_loan_c,
                (   
                    SELECT AVG(loan_amount)
                    FROM df
                    WHERE grade = 'D'
                )  AS mean_loan_d
        """
    output_dict = ps.sqldf(query).to_dict()
    for k, v in output_dict.items():
        ti.xcom_push(key=k, value=list(v.values())[0])


with DAG(
    "loan_processing",
    start_date=datetime(2023, 1, 1),
    schedule_interval="@daily",
    catchup=False,
) as dag:

    is_data_local = PythonOperator(
        task_id="is_data_local",
        python_callable=_is_data_local,
        op_kwargs={"datapath": DEST_PATH_FILE},
    )

    branch = BranchPythonOperator(task_id="branch", python_callable=_branch)

    log_data_is_local = BashOperator(
        task_id="log_data_is_local", bash_command="echo data is present locally"
    )

    log_data_is_remote = DummyOperator(task_id="log_data_is_remote")

    extract_data_from_s3 = PythonOperator(
        task_id="extract_data_from_s3",
        python_callable=_extract_from_s3,
        op_kwargs={
            "key": SOURCE_S3_KEY,
            "bucket_name": SOURCE_S3_BUCKET,
            "local_path": DEST_PATH_FILE,
        },
    )

    compute_general_aggregats = PythonOperator(
        task_id="compute_general_aggregats",
        python_callable=_compute_general_aggregats,
        trigger_rule=TriggerRule.NONE_FAILED_MIN_ONE_SUCCESS,
        op_kwargs={"datapath": os.path.join(DEST_PATH_FILE, "loan_data_small.csv")},
    )

    compute_loan_aggregats = PythonOperator(
        task_id="compute_loan_aggregats",
        python_callable=_compute_loan_aggregats,
        op_kwargs={"datapath": os.path.join(DEST_PATH_FILE, "loan_data_small.csv")},
    )

    create_aggregats_table_if_needed = PostgresOperator(
        task_id="create_table",
        postgres_conn_id="destination",
        sql="""
            CREATE TABLE IF NOT EXISTS aggregats_loans_table (
                nb_lines numeric,
                nb_cols numeric,
                date_and_hour timestamp,
                mean_loan_a numeric,
                mean_loan_b numeric,
                mean_loan_c numeric,
                mean_loan_d numeric
            
            )
        """,
    )

    store_aggregats = PostgresOperator(
        task_id="store_aggregats",
        postgres_conn_id="destination",
        sql="""
            INSERT INTO aggregats_loans_tabble (nb_lines, nb_cols, date_and_hour, mean_loan_a, mean_loan_b, mean_loan_c, mean_loan_d) VALUES
            ({{ ti.xcom_pull(key="nb_lines", task_ids="compute_general_aggregats") }},
            {{ ti.xcom_pull(key="nb_cols",  task_ids="compute_general_aggregats") }},
            CURRENT_TIMESTAMP,
            {{ ti.xcom_pull(key="mean_loan_a", task_ids="compute_loan_aggregats") }},
            {{ ti.xcom_pull(key="mean_loan_b", task_ids="compute_loan_aggregats") }},
            {{ ti.xcom_pull(key="mean_loan_c", task_ids="compute_loan_aggregats") }},
            {{ ti.xcom_pull(key="mean_loan_d", task_ids="compute_loan_aggregats") }}
                
                )

        """,
    )

is_data_local >> branch >> [log_data_is_local, log_data_is_remote]
log_data_is_remote >> extract_data_from_s3

(
    [log_data_is_local, extract_data_from_s3]
    >> compute_general_aggregats
    >> compute_loan_aggregats
    >> create_aggregats_table_if_needed
    >> store_aggregats
)