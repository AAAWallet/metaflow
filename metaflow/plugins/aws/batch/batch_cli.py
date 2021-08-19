import os
import sys
import time
import traceback

import click

from distutils.dir_util import copy_tree
from io import BytesIO

from .batch import Batch, BatchKilledException

from metaflow.datastore import FlowDataStore
from metaflow.datastore.local_backend import LocalBackend
from metaflow.metaflow_config import DATASTORE_LOCAL_DIR
from .batch import Batch, BatchKilledException

from metaflow import util
from metaflow.plugins.aws.utils import (
    CommonTaskAttrs,
    sync_local_metadata_from_datastore,
)

from metaflow import R
from metaflow.exception import (
    CommandException,
    METAFLOW_EXIT_DISALLOW_RETRY,
)
from metaflow.mflog import TASK_LOG_SOURCE



@click.group()
def cli():
    pass


@cli.group(help="Commands related to AWS Batch.")
def batch():
    pass


def _execute_cmd(func, flow_name, run_id, user, my_runs, echo):
    if user and my_runs:
        raise CommandException("--user and --my-runs are mutually exclusive.")

    if run_id and my_runs:
        raise CommandException("--run_id and --my-runs are mutually exclusive.")

    if my_runs:
        user = util.get_username()

    latest_run = True

    if user and not run_id:
        latest_run = False

    if not run_id and latest_run:
        run_id = util.get_latest_run_id(echo, flow_name)
        if run_id is None:
            raise CommandException("A previous run id was not found. Specify --run-id.")

    func(flow_name, run_id, user, echo)


@batch.command(help="List unfinished AWS Batch tasks of this flow")
@click.option("--my-runs", default=False, is_flag=True,
    help="List all my unfinished tasks.")
@click.option("--user", default=None,
    help="List unfinished tasks for the given user.")
@click.option("--run-id", default=None,
    help="List unfinished tasks corresponding to the run id.")
@click.pass_context
def list(ctx, run_id, user, my_runs):
    batch = Batch(ctx.obj.metadata, ctx.obj.environment)
    _execute_cmd(
        batch.list_jobs, ctx.obj.flow.name, run_id, user, my_runs, ctx.obj.echo
    )


@batch.command(help="Terminate unfinished AWS Batch tasks of this flow.")
@click.option("--my-runs", default=False, is_flag=True,
    help="Kill all my unfinished tasks.")
@click.option("--user", default=None,
    help="Terminate unfinished tasks for the given user.")
@click.option("--run-id", default=None,
    help="Terminate unfinished tasks corresponding to the run id.")
@click.pass_context
def kill(ctx, run_id, user, my_runs):
    batch = Batch(ctx.obj.metadata, ctx.obj.environment)
    _execute_cmd(
        batch.kill_jobs, ctx.obj.flow.name, run_id, user, my_runs, ctx.obj.echo
    )


@batch.command(
    help="Execute a single task using AWS Batch. This command "
    "calls the top-level step command inside a AWS Batch "
    "job with the given options. Typically you do not "
    "call this command directly; it is used internally "
    "by Metaflow."
)
@click.argument("step-name")
@click.argument("code-package-sha")
@click.argument("code-package-url")
@click.option("--executable", help="Executable requirement for AWS Batch.")
@click.option(
    "--image", help="Docker image requirement for AWS Batch. In name:version format."
)
@click.option(
    "--iam-role", help="IAM role requirement for AWS Batch."
)
@click.option(
    "--execution-role", help="Execution role requirement for AWS Batch on Fargate."
)
@click.option("--cpu", help="CPU requirement for AWS Batch.")
@click.option("--gpu", help="GPU requirement for AWS Batch.")
@click.option("--memory", help="Memory requirement for AWS Batch.")
@click.option("--queue", help="Job execution queue for AWS Batch.")
@click.option("--run-id", help="Passed to the top-level 'step'.")
@click.option("--task-id", help="Passed to the top-level 'step'.")
@click.option("--input-paths", help="Passed to the top-level 'step'.")
@click.option("--split-index", help="Passed to the top-level 'step'.")
@click.option("--clone-path", help="Passed to the top-level 'step'.")
@click.option("--clone-run-id", help="Passed to the top-level 'step'.")
@click.option(
    "--tag", multiple=True, default=None, help="Passed to the top-level 'step'."
)
@click.option("--namespace", default=None, help="Passed to the top-level 'step'.")
@click.option("--retry-count", default=0, help="Passed to the top-level 'step'.")
@click.option(
    "--max-user-code-retries", default=0, help="Passed to the top-level 'step'."
)
@click.option(
    "--run-time-limit",
    default=5 * 24 * 60 * 60,
    help="Run time limit in seconds for the AWS Batch job. " "Default is 5 days."
)
@click.option("--shared-memory", help="Shared Memory requirement for AWS Batch.")
@click.option("--max-swap", help="Max Swap requirement for AWS Batch.")
@click.option("--swappiness", help="Swappiness requirement for AWS Batch.")
#TODO: Maybe remove it altogether since it's not used here
@click.option('--ubf-context', default=None, type=click.Choice([None]))
@click.pass_context
def step(
    ctx,
    step_name,
    code_package_sha,
    code_package_url,
    executable=None,
    image=None,
    iam_role=None,
    execution_role=None,
    cpu=None,
    gpu=None,
    memory=None,
    queue=None,
    run_time_limit=None,
    shared_memory=None,
    max_swap=None,
    swappiness=None,
    **kwargs
):
    def echo(msg, stream='stderr', batch_id=None):
        msg = util.to_unicode(msg)
        if batch_id:
            msg = '[%s] %s' % (batch_id, msg)
        ctx.obj.echo_always(msg, err=(stream == sys.stderr))

    if R.use_r():
        entrypoint = R.entrypoint()
    else:
        if executable is None:
            executable = ctx.obj.environment.executable(step_name)
        entrypoint = '%s -u %s' % (executable,
                                   os.path.basename(sys.argv[0]))

    top_args = " ".join(util.dict_to_cli_options(ctx.parent.parent.params))

    input_paths = kwargs.get("input_paths")
    split_vars = None
    if input_paths:
        max_size = 30 * 1024
        split_vars = {
            "METAFLOW_INPUT_PATHS_%d" % (i // max_size): input_paths[i : i + max_size]
            for i in range(0, len(input_paths), max_size)
        }
        kwargs["input_paths"] = "".join("${%s}" % s for s in split_vars.keys())

    step_args = " ".join(util.dict_to_cli_options(kwargs))
    step_cli = u"{entrypoint} {top_args} step {step} {step_args}".format(
        entrypoint=entrypoint, top_args=top_args, step=step_name, step_args=step_args
    )
    node = ctx.obj.graph[step_name]

    # Get retry information
    retry_count = kwargs.get("retry_count", 0)
    retry_deco = [deco for deco in node.decorators if deco.name == "retry"]
    minutes_between_retries = None
    if retry_deco:
        minutes_between_retries = int(
            retry_deco[0].attributes.get("minutes_between_retries", 1)
        )

    common_attrs = CommonTaskAttrs(
        flow_name=ctx.obj.flow.name,
        run_id=kwargs['run_id'],
        step_name=step_name,
        task_id=kwargs['task_id'],
        attempt=retry_count,
        user=util.get_username(),
        version=ctx.obj.environment.get_environment_info()[
            "metaflow_version"
        ]
    )

    # Set batch attributes
    attrs = common_attrs.to_dict(key_prefix='metaflow.')

    env_deco = [deco for deco in node.decorators if deco.name == "environment"]
    if env_deco:
        env = env_deco[0].attributes["vars"]
    else:
        env = {}

    # Add the environment variables related to the input-paths argument
    if split_vars:
        env.update(split_vars)

    if retry_count:
        ctx.obj.echo_always(
            "Sleeping %d minutes before the next AWS Batch retry" % minutes_between_retries
        )
        time.sleep(minutes_between_retries * 60)

    # this information is needed for log tailing
    ds = ctx.obj.flow_datastore.get_task_datastore(
        mode='w',
        run_id=kwargs['run_id'],
        step_name=step_name,
        task_id=kwargs['task_id'],
        attempt=int(retry_count)
    )
    stdout_location = ds.get_log_location(TASK_LOG_SOURCE, 'stdout')
    stderr_location = ds.get_log_location(TASK_LOG_SOURCE, 'stderr')

    batch = Batch(ctx.obj.metadata, ctx.obj.environment)
    try:
        with ctx.obj.monitor.measure("metaflow.batch.launch"):
            batch.launch_job(
                flow_name=ctx.obj.flow.name,
                run_id=kwargs['run_id'],
                step_name=step_name,
                task_id=kwargs['task_id'],
                step_cli=step_cli,
                attempt=str(retry_count),
                code_package_sha=code_package_sha,
                code_package_url=code_package_url,
                code_package_ds=ctx.obj.flow_datastore.TYPE,
                image=image,
                queue=queue,
                iam_role=iam_role,
                execution_role=execution_role,
                cpu=cpu,
                gpu=gpu,
                memory=memory,
                run_time_limit=run_time_limit,
                shared_memory=shared_memory,
                max_swap=max_swap,
                swappiness=swappiness,
                env=env,
                attrs=attrs
            )
    except Exception as e:
        print(e)
        task_datastore = FlowDataStore(
            ctx.obj.flow.name, ctx.obj.environment,
            ctx.obj.metadata, ctx.obj.event_logger, ctx.obj.monitor)\
        .get_task_datastore(kwargs['run_id'], step_name, kwargs['task_id'])
        sync_local_metadata_from_datastore(ctx.obj.metadata, task_datastore)

        sys.exit(METAFLOW_EXIT_DISALLOW_RETRY)
    try:
        batch.wait(stdout_location, stderr_location, echo=echo)
    except BatchKilledException:
        # don't retry killed tasks
        traceback.print_exc()
        sys.exit(METAFLOW_EXIT_DISALLOW_RETRY)
    finally:
        task_datastore = FlowDataStore(
            ctx.obj.flow.name, ctx.obj.environment,
            ctx.obj.metadata, ctx.obj.event_logger, ctx.obj.monitor)\
            .get_task_datastore(kwargs['run_id'], step_name, kwargs['task_id'])

        sync_local_metadata_from_datastore(ctx.obj.metadata, task_datastore)
