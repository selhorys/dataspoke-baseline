FROM apache/airflow:3.1.8-python3.13

# We use Airflow 3.x's built-in SimpleAuthManager (not FAB). No extra pip
# installs needed — it's part of the base image.
#
# Auth flow:
#   1. AirflowClient calls POST /auth/token with admin creds → JWT
#   2. Subsequent REST calls use Authorization: Bearer <jwt>
#
# Bake DAGs into the image so Airflow pods read them from the container FS.
# Updating a DAG requires: edit src/workflows/dags/*.py → rebuild + push → rollout
# restart of scheduler/api-server/triggerer (Airflow's dag_dir_list_interval picks
# the new files up within 60s).
COPY --chown=airflow:root src/workflows/dags/ /opt/airflow/dags/
