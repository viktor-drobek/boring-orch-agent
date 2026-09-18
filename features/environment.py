from contextlib import ExitStack


def before_scenario(context, scenario):
    context.resources = ExitStack()


def after_scenario(context, scenario):
    context.resources.close()
