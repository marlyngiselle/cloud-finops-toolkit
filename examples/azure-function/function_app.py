"""Azure Functions (Python v2 model) timer trigger for scheduled VM shutdown.

Deploy with a system-assigned managed identity that has "Virtual Machine
Contributor" on the target scope (and Reader for Resource Graph). The
handler stays in dry-run mode until FINOPS_DRY_RUN is set to "false" in the
app settings, so a first deployment only logs what it would stop.
"""

import logging
import os

import azure.functions as func

from finops.handler import main

app = func.FunctionApp()


# Every 15 minutes. Keep FINOPS_WINDOW_MINUTES >= the timer interval so no
# shutdown time falls between two runs.
@app.timer_trigger(schedule="0 */15 * * * *", arg_name="timer", run_on_startup=False)
def scheduled_shutdown(timer: func.TimerRequest) -> None:
    if timer.past_due:
        logging.warning("Timer is running late; the window still covers the missed slot.")
    exit_code = main(dict(os.environ))
    if exit_code != 0:
        raise RuntimeError("one or more VMs failed to deallocate, see logs")
