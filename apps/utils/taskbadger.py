def update_taskbadger_data(celery_task, message_handler, message):
    tb_task = celery_task.taskbadger_task
    if tb_task:
        tb_task.safe_update(
            data={
                "experiment_id": message_handler.experiment.id,
                "identifier": message_handler.get_chat_id_from_message(message),
            },
            data_merge_strategy="default",
        )
