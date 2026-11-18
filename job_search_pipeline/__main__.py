"""`python3 -m job_search_pipeline` runs the whole pipeline; see pipeline.py for the flags."""
from job_search_pipeline.env import load_env_files


def main():
    # Before any pipeline import: modules read the environment (and config.py JOB_PIPELINE_CONFIG) at import
    load_env_files()
    from job_search_pipeline.pipeline import main as run
    run()


if __name__ == "__main__":
    main()
