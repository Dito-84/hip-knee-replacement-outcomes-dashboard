"""Alarm drill: prove each alarm reaches your inbox, without breaking anything.

The alarms only email when they go into ALARM, and most of them never have. This sets
one alarm (or all of them) to ALARM by hand, which CloudWatch treats like a real breach:
it publishes to the alert topic, so the email should arrive within a minute or two. The
alarm goes back to OK on its own at its next evaluation (about 5 minutes), because the
metric itself is fine. Nothing else changes.

Run it from infra/ with its virtual environment active, on your own AWS profile (it needs
cloudwatch:DescribeAlarms and cloudwatch:SetAlarmState, which CI's roles don't have):

    python ../scripts/alarm_drill.py              # list the stack's alarms and their state
    python ../scripts/alarm_drill.py --all        # set every one to ALARM: expect one email each
    python ../scripts/alarm_drill.py --alarm HipKneeDashboard-SiteServerErrors...

Worth doing after a deploy that adds or changes an alarm, and every few months.
"""

import argparse

import boto3

PREFIX = "HipKneeDashboard-"
REASON = "Alarm drill (scripts/alarm_drill.py): set by hand to test the email. Nothing is wrong."


def stack_alarms(cloudwatch) -> list[dict]:
    alarms = []
    for page in cloudwatch.get_paginator("describe_alarms").paginate(AlarmNamePrefix=PREFIX,
                                                                     AlarmTypes=["MetricAlarm"]):
        alarms.extend(page["MetricAlarms"])
    return alarms


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    which = parser.add_mutually_exclusive_group()
    which.add_argument("--all", action="store_true", help="set every stack alarm to ALARM")
    which.add_argument("--alarm", help="set this one alarm (its full name) to ALARM")
    parser.add_argument("--profile", default=None)
    args = parser.parse_args(argv)
    cloudwatch = boto3.Session(profile_name=args.profile).client("cloudwatch")

    alarms = stack_alarms(cloudwatch)
    names = [a["AlarmName"] for a in alarms]
    if args.alarm and args.alarm not in names:
        raise SystemExit(f"No alarm named {args.alarm}. Run without arguments to list them.")
    targets = names if args.all else [args.alarm] if args.alarm else []

    for alarm in alarms:
        actions = "emails" if alarm["AlarmActions"] else "NO ACTION: it can't email"
        print(f"{alarm['StateValue']:<17} {alarm['AlarmName']}  ({actions})")
    for name in targets:
        cloudwatch.set_alarm_state(AlarmName=name, StateValue="ALARM", StateReason=REASON)
        print(f"Set to ALARM: {name}")
    if targets:
        print(f"\nExpect {len(targets)} email(s). Each alarm returns to OK at its next evaluation.")


if __name__ == "__main__":
    main()
