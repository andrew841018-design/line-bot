# line_bot 自動迭代報告 — 2026-05-09 12:25:04 TW

[12:25:04] ===== 開始 =====
[12:25:04] ## Step 1: git pull
來自 github.com:andrew841018-design/line-bot
 * branch            main       -> FETCH_HEAD
已經是最新的。
[12:25:10] ## Step 2: pytest
........................................................................ [  9%]
........................................................................ [ 18%]
........................................................................ [ 28%]
........................F............................................... [ 37%]
..........................................F............................. [ 47%]
........................................................................ [ 56%]
........................................................................ [ 66%]
........................................................................ [ 75%]
........................................................................ [ 85%]
........................................................................ [ 94%]
......FFFFF.............................                                 [100%]
=================================== FAILURES ===================================
________________ test_below_organic_threshold_does_not_trigger _________________
tests/test_auto_trigger_pilot.py:178: in test_below_organic_threshold_does_not_trigger
    assert cond["should_trigger"] is False
E   assert True is False
_______________________ test_push_discord_no_webhook_url _______________________
tests/test_check_training_health.py:456: in test_push_discord_no_webhook_url
    assert ok is False  # 沒 url 應 graceful degrade
    ^^^^^^^^^^^^^^^^^^
E   assert True is False
----------------------------- Captured stdout call -----------------------------
Discord DM 送出成功
_____________________________ test_load_context_df _____________________________
tests/test_spark_pipeline.py:120: in test_load_context_df
    assert df.count() == 14
           ^^^^^^^^^^
.venv/lib/python3.13/site-packages/pyspark/sql/classic/dataframe.py:439: in count
    return int(self._jdf.count())
               ^^^^^^^^^^^^^^^^^
.venv/lib/python3.13/site-packages/py4j/java_gateway.py:1362: in __call__
    return_value = get_return_value(
.venv/lib/python3.13/site-packages/pyspark/errors/exceptions/captured.py:263: in deco
    return f(*a, **kw)
           ^^^^^^^^^^^
.venv/lib/python3.13/site-packages/py4j/protocol.py:327: in get_return_value
    raise Py4JJavaError(
E   py4j.protocol.Py4JJavaError: An error occurred while calling o51.count.
E   : org.apache.spark.SparkException: Job aborted due to stage failure: Task 1 in stage 0.0 failed 1 times, most recent failure: Lost task 1.0 in stage 0.0 (TID 1) (192.168.1.101 executor driver): org.apache.spark.SparkException: 
E   Error from python worker:
E     Traceback (most recent call last):
E       File "/Library/Frameworks/Python.framework/Versions/3.8/lib/python3.8/runpy.py", line 185, in _run_module_as_main
E         mod_name, mod_spec, code = _get_module_details(mod_name, _Error)
E       File "/Library/Frameworks/Python.framework/Versions/3.8/lib/python3.8/runpy.py", line 111, in _get_module_details
E         __import__(pkg_name)
E       File "<frozen importlib._bootstrap>", line 991, in _find_and_load
E       File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
E       File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
E       File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
E       File "<frozen zipimport>", line 259, in load_module
E       File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/__init__.py", line 53, in <module>
E       File "<frozen importlib._bootstrap>", line 991, in _find_and_load
E       File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
E       File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
E       File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
E       File "<frozen zipimport>", line 259, in load_module
E       File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/util.py", line 35, in <module>
E       File "<frozen importlib._bootstrap>", line 991, in _find_and_load
E       File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
E       File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
E       File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
E       File "<frozen zipimport>", line 259, in load_module
E       File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/__init__.py", line 21, in <module>
E       File "<frozen importlib._bootstrap>", line 991, in _find_and_load
E       File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
E       File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
E       File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
E       File "<frozen zipimport>", line 259, in load_module
E       File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/exceptions/base.py", line 23, in <module>
E       File "<frozen importlib._bootstrap>", line 991, in _find_and_load
E       File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
E       File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
E       File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
E       File "<frozen zipimport>", line 259, in load_module
E       File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/utils.py", line 40, in <module>
E       File "<frozen importlib._bootstrap>", line 991, in _find_and_load
E       File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
E       File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
E       File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
E       File "<frozen zipimport>", line 259, in load_module
E       File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/error_classes.py", line 26, in <module>
E     AttributeError: module 'importlib.resources' has no attribute 'files'
E   PYTHONPATH was:
E     /Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip:/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/py4j-0.10.9.9-src.zip:/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/jars/spark-core_2.13-4.1.1.jar
E   org.apache.spark.SparkException: EOFException occurred while reading the port number from pyspark.daemon's stdout.
E   	at org.apache.spark.errors.SparkCoreErrors$.eofExceptionWhileReadPortNumberError(SparkCoreErrors.scala:55)
E   	at org.apache.spark.api.python.PythonWorkerFactory.startDaemon(PythonWorkerFactory.scala:335)
E   	at org.apache.spark.api.python.PythonWorkerFactory.createThroughDaemon(PythonWorkerFactory.scala:188)
E   	at org.apache.spark.api.python.PythonWorkerFactory.create(PythonWorkerFactory.scala:152)
E   	at org.apache.spark.SparkEnv.createPythonWorker(SparkEnv.scala:158)
E   	at org.apache.spark.api.python.BasePythonRunner.compute(PythonRunner.scala:309)
E   	at org.apache.spark.api.python.PythonRDD.compute(PythonRDD.scala:72)
E   	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
E   	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
E   	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
E   	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
E   	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
E   	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
E   	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
E   	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
E   	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
E   	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
E   	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
E   	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
E   	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
E   	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
E   	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
E   	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
E   	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
E   	at org.apache.spark.scheduler.ShuffleMapTask.runTask(ShuffleMapTask.scala:107)
E   	at org.apache.spark.scheduler.ShuffleMapTask.runTask(ShuffleMapTask.scala:54)
E   	at org.apache.spark.TaskContext.runTaskWithListeners(TaskContext.scala:180)
E   	at org.apache.spark.scheduler.Task.run(Task.scala:147)
E   	at org.apache.spark.executor.Executor$TaskRunner.$anonfun$run$5(Executor.scala:716)
E   	at org.apache.spark.util.SparkErrorUtils.tryWithSafeFinally(SparkErrorUtils.scala:86)
E   	at org.apache.spark.util.SparkErrorUtils.tryWithSafeFinally$(SparkErrorUtils.scala:83)
E   	at org.apache.spark.util.Utils$.tryWithSafeFinally(Utils.scala:97)
E   	at org.apache.spark.executor.Executor$TaskRunner.run(Executor.scala:719)
E   	at java.base/java.util.concurrent.ThreadPoolExecutor.runWorker(ThreadPoolExecutor.java:1136)
E   	at java.base/java.util.concurrent.ThreadPoolExecutor$Worker.run(ThreadPoolExecutor.java:635)
E   	at java.base/java.lang.Thread.run(Thread.java:833)
E   
E   Driver stacktrace:
E   	at org.apache.spark.scheduler.DAGScheduler.$anonfun$abortStage$3(DAGScheduler.scala:3122)
E   	at scala.Option.getOrElse(Option.scala:201)
E   	at org.apache.spark.scheduler.DAGScheduler.$anonfun$abortStage$2(DAGScheduler.scala:3122)
E   	at org.apache.spark.scheduler.DAGScheduler.$anonfun$abortStage$2$adapted(DAGScheduler.scala:3114)
E   	at scala.collection.immutable.List.foreach(List.scala:323)
E   	at org.apache.spark.scheduler.DAGScheduler.abortStage(DAGScheduler.scala:3114)
E   	at org.apache.spark.scheduler.DAGScheduler.$anonfun$handleTaskSetFailed$1(DAGScheduler.scala:1303)
E   	at org.apache.spark.scheduler.DAGScheduler.$anonfun$handleTaskSetFailed$1$adapted(DAGScheduler.scala:1303)
E   	at scala.Option.foreach(Option.scala:437)
E   	at org.apache.spark.scheduler.DAGScheduler.handleTaskSetFailed(DAGScheduler.scala:1303)
E   	at org.apache.spark.scheduler.DAGSchedulerEventProcessLoop.doOnReceive(DAGScheduler.scala:3397)
E   	at org.apache.spark.scheduler.DAGSchedulerEventProcessLoop.onReceive(DAGScheduler.scala:3328)
E   	at org.apache.spark.scheduler.DAGSchedulerEventProcessLoop.onReceive(DAGScheduler.scala:3317)
E   	at org.apache.spark.util.EventLoop$$anon$1.run(EventLoop.scala:50)
E   Caused by: org.apache.spark.SparkException: 
E   Error from python worker:
E     Traceback (most recent call last):
E       File "/Library/Frameworks/Python.framework/Versions/3.8/lib/python3.8/runpy.py", line 185, in _run_module_as_main
E         mod_name, mod_spec, code = _get_module_details(mod_name, _Error)
E       File "/Library/Frameworks/Python.framework/Versions/3.8/lib/python3.8/runpy.py", line 111, in _get_module_details
E         __import__(pkg_name)
E       File "<frozen importlib._bootstrap>", line 991, in _find_and_load
E       File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
E       File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
E       File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
E       File "<frozen zipimport>", line 259, in load_module
E       File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/__init__.py", line 53, in <module>
E       File "<frozen importlib._bootstrap>", line 991, in _find_and_load
E       File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
E       File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
E       File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
E       File "<frozen zipimport>", line 259, in load_module
E       File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/util.py", line 35, in <module>
E       File "<frozen importlib._bootstrap>", line 991, in _find_and_load
E       File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
E       File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
E       File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
E       File "<frozen zipimport>", line 259, in load_module
E       File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/__init__.py", line 21, in <module>
E       File "<frozen importlib._bootstrap>", line 991, in _find_and_load
E       File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
E       File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
E       File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
E       File "<frozen zipimport>", line 259, in load_module
E       File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/exceptions/base.py", line 23, in <module>
E       File "<frozen importlib._bootstrap>", line 991, in _find_and_load
E       File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
E       File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
E       File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
E       File "<frozen zipimport>", line 259, in load_module
E       File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/utils.py", line 40, in <module>
E       File "<frozen importlib._bootstrap>", line 991, in _find_and_load
E       File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
E       File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
E       File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
E       File "<frozen zipimport>", line 259, in load_module
E       File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/error_classes.py", line 26, in <module>
E     AttributeError: module 'importlib.resources' has no attribute 'files'
E   PYTHONPATH was:
E     /Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip:/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/py4j-0.10.9.9-src.zip:/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/jars/spark-core_2.13-4.1.1.jar
E   org.apache.spark.SparkException: EOFException occurred while reading the port number from pyspark.daemon's stdout.
E   	at org.apache.spark.errors.SparkCoreErrors$.eofExceptionWhileReadPortNumberError(SparkCoreErrors.scala:55)
E   	at org.apache.spark.api.python.PythonWorkerFactory.startDaemon(PythonWorkerFactory.scala:335)
E   	at org.apache.spark.api.python.PythonWorkerFactory.createThroughDaemon(PythonWorkerFactory.scala:188)
E   	at org.apache.spark.api.python.PythonWorkerFactory.create(PythonWorkerFactory.scala:152)
E   	at org.apache.spark.SparkEnv.createPythonWorker(SparkEnv.scala:158)
E   	at org.apache.spark.api.python.BasePythonRunner.compute(PythonRunner.scala:309)
E   	at org.apache.spark.api.python.PythonRDD.compute(PythonRDD.scala:72)
E   	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
E   	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
E   	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
E   	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
E   	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
E   	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
E   	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
E   	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
E   	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
E   	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
E   	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
E   	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
E   	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
E   	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
E   	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
E   	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
E   	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
E   	at org.apache.spark.scheduler.ShuffleMapTask.runTask(ShuffleMapTask.scala:107)
E   	at org.apache.spark.scheduler.ShuffleMapTask.runTask(ShuffleMapTask.scala:54)
E   	at org.apache.spark.TaskContext.runTaskWithListeners(TaskContext.scala:180)
E   	at org.apache.spark.scheduler.Task.run(Task.scala:147)
E   	at org.apache.spark.executor.Executor$TaskRunner.$anonfun$run$5(Executor.scala:716)
E   	at org.apache.spark.util.SparkErrorUtils.tryWithSafeFinally(SparkErrorUtils.scala:86)
E   	at org.apache.spark.util.SparkErrorUtils.tryWithSafeFinally$(SparkErrorUtils.scala:83)
E   	at org.apache.spark.util.Utils$.tryWithSafeFinally(Utils.scala:97)
E   	at org.apache.spark.executor.Executor$TaskRunner.run(Executor.scala:719)
E   	at java.base/java.util.concurrent.ThreadPoolExecutor.runWorker(ThreadPoolExecutor.java:1136)
E   	at java.base/java.util.concurrent.ThreadPoolExecutor$Worker.run(ThreadPoolExecutor.java:635)
E   	at java.base/java.lang.Thread.run(Thread.java:833)
---------------------------- Captured stderr setup -----------------------------
WARNING: Using incubator modules: jdk.incubator.vector
Using Spark's default log4j profile: org/apache/spark/log4j2-defaults.properties
26/05/09 12:25:59 WARN Utils: Your hostname, MacBook-Pro-2.local, resolves to a loopback address: 127.0.0.1; using 192.168.1.101 instead (on interface en0)
26/05/09 12:25:59 WARN Utils: Set SPARK_LOCAL_IP if you need to bind to another address
Using Spark's default log4j profile: org/apache/spark/log4j2-defaults.properties
Setting default log level to "WARN".
To adjust logging level use sc.setLogLevel(newLevel). For SparkR, use setLogLevel(newLevel).
26/05/09 12:26:00 WARN NativeCodeLoader: Unable to load native-hadoop library for your platform... using builtin-java classes where applicable
----------------------------- Captured stderr call -----------------------------
26/05/09 12:26:08 ERROR Executor: Exception in task 1.0 in stage 0.0 (TID 1)
org.apache.spark.SparkException: 
Error from python worker:
  Traceback (most recent call last):
    File "/Library/Frameworks/Python.framework/Versions/3.8/lib/python3.8/runpy.py", line 185, in _run_module_as_main
      mod_name, mod_spec, code = _get_module_details(mod_name, _Error)
    File "/Library/Frameworks/Python.framework/Versions/3.8/lib/python3.8/runpy.py", line 111, in _get_module_details
      __import__(pkg_name)
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/__init__.py", line 53, in <module>
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/util.py", line 35, in <module>
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/__init__.py", line 21, in <module>
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/exceptions/base.py", line 23, in <module>
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/utils.py", line 40, in <module>
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/error_classes.py", line 26, in <module>
  AttributeError: module 'importlib.resources' has no attribute 'files'
PYTHONPATH was:
  /Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip:/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/py4j-0.10.9.9-src.zip:/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/jars/spark-core_2.13-4.1.1.jar
org.apache.spark.SparkException: EOFException occurred while reading the port number from pyspark.daemon's stdout.
	at org.apache.spark.errors.SparkCoreErrors$.eofExceptionWhileReadPortNumberError(SparkCoreErrors.scala:55)
	at org.apache.spark.api.python.PythonWorkerFactory.startDaemon(PythonWorkerFactory.scala:335)
	at org.apache.spark.api.python.PythonWorkerFactory.createThroughDaemon(PythonWorkerFactory.scala:188)
	at org.apache.spark.api.python.PythonWorkerFactory.create(PythonWorkerFactory.scala:152)
	at org.apache.spark.SparkEnv.createPythonWorker(SparkEnv.scala:158)
	at org.apache.spark.api.python.BasePythonRunner.compute(PythonRunner.scala:309)
	at org.apache.spark.api.python.PythonRDD.compute(PythonRDD.scala:72)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.scheduler.ShuffleMapTask.runTask(ShuffleMapTask.scala:107)
	at org.apache.spark.scheduler.ShuffleMapTask.runTask(ShuffleMapTask.scala:54)
	at org.apache.spark.TaskContext.runTaskWithListeners(TaskContext.scala:180)
	at org.apache.spark.scheduler.Task.run(Task.scala:147)
	at org.apache.spark.executor.Executor$TaskRunner.$anonfun$run$5(Executor.scala:716)
	at org.apache.spark.util.SparkErrorUtils.tryWithSafeFinally(SparkErrorUtils.scala:86)
	at org.apache.spark.util.SparkErrorUtils.tryWithSafeFinally$(SparkErrorUtils.scala:83)
	at org.apache.spark.util.Utils$.tryWithSafeFinally(Utils.scala:97)
	at org.apache.spark.executor.Executor$TaskRunner.run(Executor.scala:719)
	at java.base/java.util.concurrent.ThreadPoolExecutor.runWorker(ThreadPoolExecutor.java:1136)
	at java.base/java.util.concurrent.ThreadPoolExecutor$Worker.run(ThreadPoolExecutor.java:635)
	at java.base/java.lang.Thread.run(Thread.java:833)
26/05/09 12:26:08 WARN TaskSetManager: Lost task 1.0 in stage 0.0 (TID 1) (192.168.1.101 executor driver): org.apache.spark.SparkException: 
Error from python worker:
  Traceback (most recent call last):
    File "/Library/Frameworks/Python.framework/Versions/3.8/lib/python3.8/runpy.py", line 185, in _run_module_as_main
      mod_name, mod_spec, code = _get_module_details(mod_name, _Error)
    File "/Library/Frameworks/Python.framework/Versions/3.8/lib/python3.8/runpy.py", line 111, in _get_module_details
      __import__(pkg_name)
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/__init__.py", line 53, in <module>
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/util.py", line 35, in <module>
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/__init__.py", line 21, in <module>
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/exceptions/base.py", line 23, in <module>
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/utils.py", line 40, in <module>
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/error_classes.py", line 26, in <module>
  AttributeError: module 'importlib.resources' has no attribute 'files'
PYTHONPATH was:
  /Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip:/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/py4j-0.10.9.9-src.zip:/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/jars/spark-core_2.13-4.1.1.jar
org.apache.spark.SparkException: EOFException occurred while reading the port number from pyspark.daemon's stdout.
	at org.apache.spark.errors.SparkCoreErrors$.eofExceptionWhileReadPortNumberError(SparkCoreErrors.scala:55)
	at org.apache.spark.api.python.PythonWorkerFactory.startDaemon(PythonWorkerFactory.scala:335)
	at org.apache.spark.api.python.PythonWorkerFactory.createThroughDaemon(PythonWorkerFactory.scala:188)
	at org.apache.spark.api.python.PythonWorkerFactory.create(PythonWorkerFactory.scala:152)
	at org.apache.spark.SparkEnv.createPythonWorker(SparkEnv.scala:158)
	at org.apache.spark.api.python.BasePythonRunner.compute(PythonRunner.scala:309)
	at org.apache.spark.api.python.PythonRDD.compute(PythonRDD.scala:72)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.scheduler.ShuffleMapTask.runTask(ShuffleMapTask.scala:107)
	at org.apache.spark.scheduler.ShuffleMapTask.runTask(ShuffleMapTask.scala:54)
	at org.apache.spark.TaskContext.runTaskWithListeners(TaskContext.scala:180)
	at org.apache.spark.scheduler.Task.run(Task.scala:147)
	at org.apache.spark.executor.Executor$TaskRunner.$anonfun$run$5(Executor.scala:716)
	at org.apache.spark.util.SparkErrorUtils.tryWithSafeFinally(SparkErrorUtils.scala:86)
	at org.apache.spark.util.SparkErrorUtils.tryWithSafeFinally$(SparkErrorUtils.scala:83)
	at org.apache.spark.util.Utils$.tryWithSafeFinally(Utils.scala:97)
	at org.apache.spark.executor.Executor$TaskRunner.run(Executor.scala:719)
	at java.base/java.util.concurrent.ThreadPoolExecutor.runWorker(ThreadPoolExecutor.java:1136)
	at java.base/java.util.concurrent.ThreadPoolExecutor$Worker.run(ThreadPoolExecutor.java:635)
	at java.base/java.lang.Thread.run(Thread.java:833)

26/05/09 12:26:08 ERROR TaskSetManager: Task 1 in stage 0.0 failed 1 times; aborting job
_______________________ test_build_pairs_df_window_logic _______________________
tests/test_spark_pipeline.py:127: in test_build_pairs_df_window_logic
    pdf = pairs.toPandas()
          ^^^^^^^^^^^^^^^^
.venv/lib/python3.13/site-packages/pyspark/sql/classic/dataframe.py:1816: in toPandas
    return PandasConversionMixin.toPandas(self)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
.venv/lib/python3.13/site-packages/pyspark/sql/pandas/conversion.py:188: in toPandas
    rows = self.collect()
           ^^^^^^^^^^^^^^
.venv/lib/python3.13/site-packages/pyspark/sql/classic/dataframe.py:443: in collect
    sock_info = self._jdf.collectToPython()
                ^^^^^^^^^^^^^^^^^^^^^^^^^^^
.venv/lib/python3.13/site-packages/py4j/java_gateway.py:1362: in __call__
    return_value = get_return_value(
.venv/lib/python3.13/site-packages/pyspark/errors/exceptions/captured.py:263: in deco
    return f(*a, **kw)
           ^^^^^^^^^^^
.venv/lib/python3.13/site-packages/py4j/protocol.py:327: in get_return_value
    raise Py4JJavaError(
E   py4j.protocol.Py4JJavaError: An error occurred while calling o138.collectToPython.
E   : org.apache.spark.SparkException: Job aborted due to stage failure: Task 1 in stage 1.0 failed 1 times, most recent failure: Lost task 1.0 in stage 1.0 (TID 3) (192.168.1.101 executor driver): org.apache.spark.SparkException: 
E   Error from python worker:
E     Traceback (most recent call last):
E       File "/Library/Frameworks/Python.framework/Versions/3.8/lib/python3.8/runpy.py", line 185, in _run_module_as_main
E         mod_name, mod_spec, code = _get_module_details(mod_name, _Error)
E       File "/Library/Frameworks/Python.framework/Versions/3.8/lib/python3.8/runpy.py", line 111, in _get_module_details
E         __import__(pkg_name)
E       File "<frozen importlib._bootstrap>", line 991, in _find_and_load
E       File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
E       File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
E       File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
E       File "<frozen zipimport>", line 259, in load_module
E       File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/__init__.py", line 53, in <module>
E       File "<frozen importlib._bootstrap>", line 991, in _find_and_load
E       File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
E       File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
E       File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
E       File "<frozen zipimport>", line 259, in load_module
E       File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/util.py", line 35, in <module>
E       File "<frozen importlib._bootstrap>", line 991, in _find_and_load
E       File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
E       File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
E       File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
E       File "<frozen zipimport>", line 259, in load_module
E       File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/__init__.py", line 21, in <module>
E       File "<frozen importlib._bootstrap>", line 991, in _find_and_load
E       File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
E       File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
E       File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
E       File "<frozen zipimport>", line 259, in load_module
E       File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/exceptions/base.py", line 23, in <module>
E       File "<frozen importlib._bootstrap>", line 991, in _find_and_load
E       File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
E       File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
E       File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
E       File "<frozen zipimport>", line 259, in load_module
E       File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/utils.py", line 40, in <module>
E       File "<frozen importlib._bootstrap>", line 991, in _find_and_load
E       File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
E       File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
E       File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
E       File "<frozen zipimport>", line 259, in load_module
E       File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/error_classes.py", line 26, in <module>
E     AttributeError: module 'importlib.resources' has no attribute 'files'
E   PYTHONPATH was:
E     /Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip:/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/py4j-0.10.9.9-src.zip:/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/jars/spark-core_2.13-4.1.1.jar
E   org.apache.spark.SparkException: EOFException occurred while reading the port number from pyspark.daemon's stdout and terminated with code: 1..
E   	at org.apache.spark.errors.SparkCoreErrors$.eofExceptionWhileReadPortNumberError(SparkCoreErrors.scala:55)
E   	at org.apache.spark.api.python.PythonWorkerFactory.startDaemon(PythonWorkerFactory.scala:339)
E   	at org.apache.spark.api.python.PythonWorkerFactory.createThroughDaemon(PythonWorkerFactory.scala:188)
E   	at org.apache.spark.api.python.PythonWorkerFactory.create(PythonWorkerFactory.scala:152)
E   	at org.apache.spark.SparkEnv.createPythonWorker(SparkEnv.scala:158)
E   	at org.apache.spark.api.python.BasePythonRunner.compute(PythonRunner.scala:309)
E   	at org.apache.spark.api.python.PythonRDD.compute(PythonRDD.scala:72)
E   	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
E   	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
E   	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
E   	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
E   	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
E   	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
E   	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
E   	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
E   	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
E   	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
E   	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
E   	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
E   	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
E   	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
E   	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
E   	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
E   	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
E   	at org.apache.spark.scheduler.ShuffleMapTask.runTask(ShuffleMapTask.scala:107)
E   	at org.apache.spark.scheduler.ShuffleMapTask.runTask(ShuffleMapTask.scala:54)
E   	at org.apache.spark.TaskContext.runTaskWithListeners(TaskContext.scala:180)
E   	at org.apache.spark.scheduler.Task.run(Task.scala:147)
E   	at org.apache.spark.executor.Executor$TaskRunner.$anonfun$run$5(Executor.scala:716)
E   	at org.apache.spark.util.SparkErrorUtils.tryWithSafeFinally(SparkErrorUtils.scala:86)
E   	at org.apache.spark.util.SparkErrorUtils.tryWithSafeFinally$(SparkErrorUtils.scala:83)
E   	at org.apache.spark.util.Utils$.tryWithSafeFinally(Utils.scala:97)
E   	at org.apache.spark.executor.Executor$TaskRunner.run(Executor.scala:719)
E   	at java.base/java.util.concurrent.ThreadPoolExecutor.runWorker(ThreadPoolExecutor.java:1136)
E   	at java.base/java.util.concurrent.ThreadPoolExecutor$Worker.run(ThreadPoolExecutor.java:635)
E   	at java.base/java.lang.Thread.run(Thread.java:833)
E   
E   Driver stacktrace:
E   	at org.apache.spark.scheduler.DAGScheduler.$anonfun$abortStage$3(DAGScheduler.scala:3122)
E   	at scala.Option.getOrElse(Option.scala:201)
E   	at org.apache.spark.scheduler.DAGScheduler.$anonfun$abortStage$2(DAGScheduler.scala:3122)
E   	at org.apache.spark.scheduler.DAGScheduler.$anonfun$abortStage$2$adapted(DAGScheduler.scala:3114)
E   	at scala.collection.immutable.List.foreach(List.scala:323)
E   	at org.apache.spark.scheduler.DAGScheduler.abortStage(DAGScheduler.scala:3114)
E   	at org.apache.spark.scheduler.DAGScheduler.$anonfun$handleTaskSetFailed$1(DAGScheduler.scala:1303)
E   	at org.apache.spark.scheduler.DAGScheduler.$anonfun$handleTaskSetFailed$1$adapted(DAGScheduler.scala:1303)
E   	at scala.Option.foreach(Option.scala:437)
E   	at org.apache.spark.scheduler.DAGScheduler.handleTaskSetFailed(DAGScheduler.scala:1303)
E   	at org.apache.spark.scheduler.DAGSchedulerEventProcessLoop.doOnReceive(DAGScheduler.scala:3397)
E   	at org.apache.spark.scheduler.DAGSchedulerEventProcessLoop.onReceive(DAGScheduler.scala:3328)
E   	at org.apache.spark.scheduler.DAGSchedulerEventProcessLoop.onReceive(DAGScheduler.scala:3317)
E   	at org.apache.spark.util.EventLoop$$anon$1.run(EventLoop.scala:50)
E   Caused by: org.apache.spark.SparkException: 
E   Error from python worker:
E     Traceback (most recent call last):
E       File "/Library/Frameworks/Python.framework/Versions/3.8/lib/python3.8/runpy.py", line 185, in _run_module_as_main
E         mod_name, mod_spec, code = _get_module_details(mod_name, _Error)
E       File "/Library/Frameworks/Python.framework/Versions/3.8/lib/python3.8/runpy.py", line 111, in _get_module_details
E         __import__(pkg_name)
E       File "<frozen importlib._bootstrap>", line 991, in _find_and_load
E       File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
E       File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
E       File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
E       File "<frozen zipimport>", line 259, in load_module
E       File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/__init__.py", line 53, in <module>
E       File "<frozen importlib._bootstrap>", line 991, in _find_and_load
E       File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
E       File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
E       File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
E       File "<frozen zipimport>", line 259, in load_module
E       File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/util.py", line 35, in <module>
E       File "<frozen importlib._bootstrap>", line 991, in _find_and_load
E       File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
E       File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
E       File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
E       File "<frozen zipimport>", line 259, in load_module
E       File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/__init__.py", line 21, in <module>
E       File "<frozen importlib._bootstrap>", line 991, in _find_and_load
E       File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
E       File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
E       File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
E       File "<frozen zipimport>", line 259, in load_module
E       File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/exceptions/base.py", line 23, in <module>
E       File "<frozen importlib._bootstrap>", line 991, in _find_and_load
E       File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
E       File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
E       File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
E       File "<frozen zipimport>", line 259, in load_module
E       File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/utils.py", line 40, in <module>
E       File "<frozen importlib._bootstrap>", line 991, in _find_and_load
E       File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
E       File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
E       File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
E       File "<frozen zipimport>", line 259, in load_module
E       File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/error_classes.py", line 26, in <module>
E     AttributeError: module 'importlib.resources' has no attribute 'files'
E   PYTHONPATH was:
E     /Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip:/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/py4j-0.10.9.9-src.zip:/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/jars/spark-core_2.13-4.1.1.jar
E   org.apache.spark.SparkException: EOFException occurred while reading the port number from pyspark.daemon's stdout and terminated with code: 1..
E   	at org.apache.spark.errors.SparkCoreErrors$.eofExceptionWhileReadPortNumberError(SparkCoreErrors.scala:55)
E   	at org.apache.spark.api.python.PythonWorkerFactory.startDaemon(PythonWorkerFactory.scala:339)
E   	at org.apache.spark.api.python.PythonWorkerFactory.createThroughDaemon(PythonWorkerFactory.scala:188)
E   	at org.apache.spark.api.python.PythonWorkerFactory.create(PythonWorkerFactory.scala:152)
E   	at org.apache.spark.SparkEnv.createPythonWorker(SparkEnv.scala:158)
E   	at org.apache.spark.api.python.BasePythonRunner.compute(PythonRunner.scala:309)
E   	at org.apache.spark.api.python.PythonRDD.compute(PythonRDD.scala:72)
E   	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
E   	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
E   	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
E   	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
E   	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
E   	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
E   	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
E   	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
E   	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
E   	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
E   	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
E   	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
E   	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
E   	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
E   	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
E   	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
E   	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
E   	at org.apache.spark.scheduler.ShuffleMapTask.runTask(ShuffleMapTask.scala:107)
E   	at org.apache.spark.scheduler.ShuffleMapTask.runTask(ShuffleMapTask.scala:54)
E   	at org.apache.spark.TaskContext.runTaskWithListeners(TaskContext.scala:180)
E   	at org.apache.spark.scheduler.Task.run(Task.scala:147)
E   	at org.apache.spark.executor.Executor$TaskRunner.$anonfun$run$5(Executor.scala:716)
E   	at org.apache.spark.util.SparkErrorUtils.tryWithSafeFinally(SparkErrorUtils.scala:86)
E   	at org.apache.spark.util.SparkErrorUtils.tryWithSafeFinally$(SparkErrorUtils.scala:83)
E   	at org.apache.spark.util.Utils$.tryWithSafeFinally(Utils.scala:97)
E   	at org.apache.spark.executor.Executor$TaskRunner.run(Executor.scala:719)
E   	at java.base/java.util.concurrent.ThreadPoolExecutor.runWorker(ThreadPoolExecutor.java:1136)
E   	at java.base/java.util.concurrent.ThreadPoolExecutor$Worker.run(ThreadPoolExecutor.java:635)
E   	at java.base/java.lang.Thread.run(Thread.java:833)
----------------------------- Captured stderr call -----------------------------
26/05/09 12:26:09 WARN TaskSetManager: Lost task 0.0 in stage 0.0 (TID 0) (192.168.1.101 executor driver): TaskKilled (Stage cancelled: Job aborted due to stage failure: Task 1 in stage 0.0 failed 1 times, most recent failure: Lost task 1.0 in stage 0.0 (TID 1) (192.168.1.101 executor driver): org.apache.spark.SparkException: 
Error from python worker:
  Traceback (most recent call last):
    File "/Library/Frameworks/Python.framework/Versions/3.8/lib/python3.8/runpy.py", line 185, in _run_module_as_main
      mod_name, mod_spec, code = _get_module_details(mod_name, _Error)
    File "/Library/Frameworks/Python.framework/Versions/3.8/lib/python3.8/runpy.py", line 111, in _get_module_details
      __import__(pkg_name)
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/__init__.py", line 53, in <module>
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/util.py", line 35, in <module>
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/__init__.py", line 21, in <module>
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/exceptions/base.py", line 23, in <module>
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/utils.py", line 40, in <module>
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/error_classes.py", line 26, in <module>
  AttributeError: module 'importlib.resources' has no attribute 'files'
PYTHONPATH was:
  /Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip:/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/py4j-0.10.9.9-src.zip:/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/jars/spark-core_2.13-4.1.1.jar
org.apache.spark.SparkException: EOFException occurred while reading the port number from pyspark.daemon's stdout.
	at org.apache.spark.errors.SparkCoreErrors$.eofExceptionWhileReadPortNumberError(SparkCoreErrors.scala:55)
	at org.apache.spark.api.python.PythonWorkerFactory.startDaemon(PythonWorkerFactory.scala:335)
	at org.apache.spark.api.python.PythonWorkerFactory.createThroughDaemon(PythonWorkerFactory.scala:188)
	at org.apache.spark.api.python.PythonWorkerFactory.create(PythonWorkerFactory.scala:152)
	at org.apache.spark.SparkEnv.createPythonWorker(SparkEnv.scala:158)
	at org.apache.spark.api.python.BasePythonRunner.compute(PythonRunner.scala:309)
	at org.apache.spark.api.python.PythonRDD.compute(PythonRDD.scala:72)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.scheduler.ShuffleMapTask.runTask(ShuffleMapTask.scala:107)
	at org.apache.spark.scheduler.ShuffleMapTask.runTask(ShuffleMapTask.scala:54)
	at org.apache.spark.TaskContext.runTaskWithListeners(TaskContext.scala:180)
	at org.apache.spark.scheduler.Task.run(Task.scala:147)
	at org.apache.spark.executor.Executor$TaskRunner.$anonfun$run$5(Executor.scala:716)
	at org.apache.spark.util.SparkErrorUtils.tryWithSafeFinally(SparkErrorUtils.scala:86)
	at org.apache.spark.util.SparkErrorUtils.tryWithSafeFinally$(SparkErrorUtils.scala:83)
	at org.apache.spark.util.Utils$.tryWithSafeFinally(Utils.scala:97)
	at org.apache.spark.executor.Executor$TaskRunner.run(Executor.scala:719)
	at java.base/java.util.concurrent.ThreadPoolExecutor.runWorker(ThreadPoolExecutor.java:1136)
	at java.base/java.util.concurrent.ThreadPoolExecutor$Worker.run(ThreadPoolExecutor.java:635)
	at java.base/java.lang.Thread.run(Thread.java:833)

Driver stacktrace:)
26/05/09 12:26:09 ERROR Executor: Exception in task 1.0 in stage 1.0 (TID 3)
org.apache.spark.SparkException: 
Error from python worker:
  Traceback (most recent call last):
    File "/Library/Frameworks/Python.framework/Versions/3.8/lib/python3.8/runpy.py", line 185, in _run_module_as_main
      mod_name, mod_spec, code = _get_module_details(mod_name, _Error)
    File "/Library/Frameworks/Python.framework/Versions/3.8/lib/python3.8/runpy.py", line 111, in _get_module_details
      __import__(pkg_name)
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/__init__.py", line 53, in <module>
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/util.py", line 35, in <module>
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/__init__.py", line 21, in <module>
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/exceptions/base.py", line 23, in <module>
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/utils.py", line 40, in <module>
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/error_classes.py", line 26, in <module>
  AttributeError: module 'importlib.resources' has no attribute 'files'
PYTHONPATH was:
  /Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip:/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/py4j-0.10.9.9-src.zip:/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/jars/spark-core_2.13-4.1.1.jar
org.apache.spark.SparkException: EOFException occurred while reading the port number from pyspark.daemon's stdout and terminated with code: 1..
	at org.apache.spark.errors.SparkCoreErrors$.eofExceptionWhileReadPortNumberError(SparkCoreErrors.scala:55)
	at org.apache.spark.api.python.PythonWorkerFactory.startDaemon(PythonWorkerFactory.scala:339)
	at org.apache.spark.api.python.PythonWorkerFactory.createThroughDaemon(PythonWorkerFactory.scala:188)
	at org.apache.spark.api.python.PythonWorkerFactory.create(PythonWorkerFactory.scala:152)
	at org.apache.spark.SparkEnv.createPythonWorker(SparkEnv.scala:158)
	at org.apache.spark.api.python.BasePythonRunner.compute(PythonRunner.scala:309)
	at org.apache.spark.api.python.PythonRDD.compute(PythonRDD.scala:72)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.scheduler.ShuffleMapTask.runTask(ShuffleMapTask.scala:107)
	at org.apache.spark.scheduler.ShuffleMapTask.runTask(ShuffleMapTask.scala:54)
	at org.apache.spark.TaskContext.runTaskWithListeners(TaskContext.scala:180)
	at org.apache.spark.scheduler.Task.run(Task.scala:147)
	at org.apache.spark.executor.Executor$TaskRunner.$anonfun$run$5(Executor.scala:716)
	at org.apache.spark.util.SparkErrorUtils.tryWithSafeFinally(SparkErrorUtils.scala:86)
	at org.apache.spark.util.SparkErrorUtils.tryWithSafeFinally$(SparkErrorUtils.scala:83)
	at org.apache.spark.util.Utils$.tryWithSafeFinally(Utils.scala:97)
	at org.apache.spark.executor.Executor$TaskRunner.run(Executor.scala:719)
	at java.base/java.util.concurrent.ThreadPoolExecutor.runWorker(ThreadPoolExecutor.java:1136)
	at java.base/java.util.concurrent.ThreadPoolExecutor$Worker.run(ThreadPoolExecutor.java:635)
	at java.base/java.lang.Thread.run(Thread.java:833)
26/05/09 12:26:09 WARN TaskSetManager: Lost task 1.0 in stage 1.0 (TID 3) (192.168.1.101 executor driver): org.apache.spark.SparkException: 
Error from python worker:
  Traceback (most recent call last):
    File "/Library/Frameworks/Python.framework/Versions/3.8/lib/python3.8/runpy.py", line 185, in _run_module_as_main
      mod_name, mod_spec, code = _get_module_details(mod_name, _Error)
    File "/Library/Frameworks/Python.framework/Versions/3.8/lib/python3.8/runpy.py", line 111, in _get_module_details
      __import__(pkg_name)
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/__init__.py", line 53, in <module>
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/util.py", line 35, in <module>
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/__init__.py", line 21, in <module>
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/exceptions/base.py", line 23, in <module>
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/utils.py", line 40, in <module>
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/error_classes.py", line 26, in <module>
  AttributeError: module 'importlib.resources' has no attribute 'files'
PYTHONPATH was:
  /Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip:/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/py4j-0.10.9.9-src.zip:/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/jars/spark-core_2.13-4.1.1.jar
org.apache.spark.SparkException: EOFException occurred while reading the port number from pyspark.daemon's stdout and terminated with code: 1..
	at org.apache.spark.errors.SparkCoreErrors$.eofExceptionWhileReadPortNumberError(SparkCoreErrors.scala:55)
	at org.apache.spark.api.python.PythonWorkerFactory.startDaemon(PythonWorkerFactory.scala:339)
	at org.apache.spark.api.python.PythonWorkerFactory.createThroughDaemon(PythonWorkerFactory.scala:188)
	at org.apache.spark.api.python.PythonWorkerFactory.create(PythonWorkerFactory.scala:152)
	at org.apache.spark.SparkEnv.createPythonWorker(SparkEnv.scala:158)
	at org.apache.spark.api.python.BasePythonRunner.compute(PythonRunner.scala:309)
	at org.apache.spark.api.python.PythonRDD.compute(PythonRDD.scala:72)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.scheduler.ShuffleMapTask.runTask(ShuffleMapTask.scala:107)
	at org.apache.spark.scheduler.ShuffleMapTask.runTask(ShuffleMapTask.scala:54)
	at org.apache.spark.TaskContext.runTaskWithListeners(TaskContext.scala:180)
	at org.apache.spark.scheduler.Task.run(Task.scala:147)
	at org.apache.spark.executor.Executor$TaskRunner.$anonfun$run$5(Executor.scala:716)
	at org.apache.spark.util.SparkErrorUtils.tryWithSafeFinally(SparkErrorUtils.scala:86)
	at org.apache.spark.util.SparkErrorUtils.tryWithSafeFinally$(SparkErrorUtils.scala:83)
	at org.apache.spark.util.Utils$.tryWithSafeFinally(Utils.scala:97)
	at org.apache.spark.executor.Executor$TaskRunner.run(Executor.scala:719)
	at java.base/java.util.concurrent.ThreadPoolExecutor.runWorker(ThreadPoolExecutor.java:1136)
	at java.base/java.util.concurrent.ThreadPoolExecutor$Worker.run(ThreadPoolExecutor.java:635)
	at java.base/java.lang.Thread.run(Thread.java:833)

26/05/09 12:26:09 ERROR TaskSetManager: Task 1 in stage 1.0 failed 1 times; aborting job
_____________________ test_full_pipeline_dedup_and_quality _____________________
tests/test_spark_pipeline.py:139: in test_full_pipeline_dedup_and_quality
    rows_in, rows_out, elapsed, ppath, jpath = sp.run_pipeline(
finetune/spark_pipeline.py:342: in run_pipeline
    rows_in = df.count()
              ^^^^^^^^^^
.venv/lib/python3.13/site-packages/pyspark/sql/classic/dataframe.py:439: in count
    return int(self._jdf.count())
               ^^^^^^^^^^^^^^^^^
.venv/lib/python3.13/site-packages/py4j/java_gateway.py:1362: in __call__
    return_value = get_return_value(
.venv/lib/python3.13/site-packages/pyspark/errors/exceptions/captured.py:263: in deco
    return f(*a, **kw)
           ^^^^^^^^^^^
.venv/lib/python3.13/site-packages/py4j/protocol.py:327: in get_return_value
    raise Py4JJavaError(
E   py4j.protocol.Py4JJavaError: An error occurred while calling o163.count.
E   : org.apache.spark.SparkException: Job aborted due to stage failure: Task 0 in stage 2.0 failed 1 times, most recent failure: Lost task 0.0 in stage 2.0 (TID 4) (192.168.1.101 executor driver): org.apache.spark.SparkException: 
E   Error from python worker:
E     Traceback (most recent call last):
E       File "/Library/Frameworks/Python.framework/Versions/3.8/lib/python3.8/runpy.py", line 185, in _run_module_as_main
E         mod_name, mod_spec, code = _get_module_details(mod_name, _Error)
E       File "/Library/Frameworks/Python.framework/Versions/3.8/lib/python3.8/runpy.py", line 111, in _get_module_details
E         __import__(pkg_name)
E       File "<frozen importlib._bootstrap>", line 991, in _find_and_load
E       File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
E       File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
E       File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
E       File "<frozen zipimport>", line 259, in load_module
E       File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/__init__.py", line 53, in <module>
E       File "<frozen importlib._bootstrap>", line 991, in _find_and_load
E       File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
E       File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
E       File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
E       File "<frozen zipimport>", line 259, in load_module
E       File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/util.py", line 35, in <module>
E       File "<frozen importlib._bootstrap>", line 991, in _find_and_load
E       File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
E       File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
E       File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
E       File "<frozen zipimport>", line 259, in load_module
E       File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/__init__.py", line 21, in <module>
E       File "<frozen importlib._bootstrap>", line 991, in _find_and_load
E       File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
E       File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
E       File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
E       File "<frozen zipimport>", line 259, in load_module
E       File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/exceptions/base.py", line 23, in <module>
E       File "<frozen importlib._bootstrap>", line 991, in _find_and_load
E       File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
E       File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
E       File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
E       File "<frozen zipimport>", line 259, in load_module
E       File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/utils.py", line 40, in <module>
E       File "<frozen importlib._bootstrap>", line 991, in _find_and_load
E       File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
E       File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
E       File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
E       File "<frozen zipimport>", line 259, in load_module
E       File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/error_classes.py", line 26, in <module>
E     AttributeError: module 'importlib.resources' has no attribute 'files'
E   PYTHONPATH was:
E     /Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip:/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/py4j-0.10.9.9-src.zip:/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/jars/spark-core_2.13-4.1.1.jar
E   org.apache.spark.SparkException: EOFException occurred while reading the port number from pyspark.daemon's stdout.
E   	at org.apache.spark.errors.SparkCoreErrors$.eofExceptionWhileReadPortNumberError(SparkCoreErrors.scala:55)
E   	at org.apache.spark.api.python.PythonWorkerFactory.startDaemon(PythonWorkerFactory.scala:335)
E   	at org.apache.spark.api.python.PythonWorkerFactory.createThroughDaemon(PythonWorkerFactory.scala:188)
E   	at org.apache.spark.api.python.PythonWorkerFactory.create(PythonWorkerFactory.scala:152)
E   	at org.apache.spark.SparkEnv.createPythonWorker(SparkEnv.scala:158)
E   	at org.apache.spark.api.python.BasePythonRunner.compute(PythonRunner.scala:309)
E   	at org.apache.spark.api.python.PythonRDD.compute(PythonRDD.scala:72)
E   	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
E   	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
E   	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
E   	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
E   	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
E   	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
E   	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
E   	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
E   	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
E   	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
E   	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
E   	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
E   	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
E   	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
E   	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
E   	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
E   	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
E   	at org.apache.spark.scheduler.ShuffleMapTask.runTask(ShuffleMapTask.scala:107)
E   	at org.apache.spark.scheduler.ShuffleMapTask.runTask(ShuffleMapTask.scala:54)
E   	at org.apache.spark.TaskContext.runTaskWithListeners(TaskContext.scala:180)
E   	at org.apache.spark.scheduler.Task.run(Task.scala:147)
E   	at org.apache.spark.executor.Executor$TaskRunner.$anonfun$run$5(Executor.scala:716)
E   	at org.apache.spark.util.SparkErrorUtils.tryWithSafeFinally(SparkErrorUtils.scala:86)
E   	at org.apache.spark.util.SparkErrorUtils.tryWithSafeFinally$(SparkErrorUtils.scala:83)
E   	at org.apache.spark.util.Utils$.tryWithSafeFinally(Utils.scala:97)
E   	at org.apache.spark.executor.Executor$TaskRunner.run(Executor.scala:719)
E   	at java.base/java.util.concurrent.ThreadPoolExecutor.runWorker(ThreadPoolExecutor.java:1136)
E   	at java.base/java.util.concurrent.ThreadPoolExecutor$Worker.run(ThreadPoolExecutor.java:635)
E   	at java.base/java.lang.Thread.run(Thread.java:833)
E   
E   Driver stacktrace:
E   	at org.apache.spark.scheduler.DAGScheduler.$anonfun$abortStage$3(DAGScheduler.scala:3122)
E   	at scala.Option.getOrElse(Option.scala:201)
E   	at org.apache.spark.scheduler.DAGScheduler.$anonfun$abortStage$2(DAGScheduler.scala:3122)
E   	at org.apache.spark.scheduler.DAGScheduler.$anonfun$abortStage$2$adapted(DAGScheduler.scala:3114)
E   	at scala.collection.immutable.List.foreach(List.scala:323)
E   	at org.apache.spark.scheduler.DAGScheduler.abortStage(DAGScheduler.scala:3114)
E   	at org.apache.spark.scheduler.DAGScheduler.$anonfun$handleTaskSetFailed$1(DAGScheduler.scala:1303)
E   	at org.apache.spark.scheduler.DAGScheduler.$anonfun$handleTaskSetFailed$1$adapted(DAGScheduler.scala:1303)
E   	at scala.Option.foreach(Option.scala:437)
E   	at org.apache.spark.scheduler.DAGScheduler.handleTaskSetFailed(DAGScheduler.scala:1303)
E   	at org.apache.spark.scheduler.DAGSchedulerEventProcessLoop.doOnReceive(DAGScheduler.scala:3397)
E   	at org.apache.spark.scheduler.DAGSchedulerEventProcessLoop.onReceive(DAGScheduler.scala:3328)
E   	at org.apache.spark.scheduler.DAGSchedulerEventProcessLoop.onReceive(DAGScheduler.scala:3317)
E   	at org.apache.spark.util.EventLoop$$anon$1.run(EventLoop.scala:50)
E   Caused by: org.apache.spark.SparkException: 
E   Error from python worker:
E     Traceback (most recent call last):
E       File "/Library/Frameworks/Python.framework/Versions/3.8/lib/python3.8/runpy.py", line 185, in _run_module_as_main
E         mod_name, mod_spec, code = _get_module_details(mod_name, _Error)
E       File "/Library/Frameworks/Python.framework/Versions/3.8/lib/python3.8/runpy.py", line 111, in _get_module_details
E         __import__(pkg_name)
E       File "<frozen importlib._bootstrap>", line 991, in _find_and_load
E       File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
E       File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
E       File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
E       File "<frozen zipimport>", line 259, in load_module
E       File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/__init__.py", line 53, in <module>
E       File "<frozen importlib._bootstrap>", line 991, in _find_and_load
E       File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
E       File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
E       File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
E       File "<frozen zipimport>", line 259, in load_module
E       File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/util.py", line 35, in <module>
E       File "<frozen importlib._bootstrap>", line 991, in _find_and_load
E       File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
E       File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
E       File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
E       File "<frozen zipimport>", line 259, in load_module
E       File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/__init__.py", line 21, in <module>
E       File "<frozen importlib._bootstrap>", line 991, in _find_and_load
E       File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
E       File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
E       File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
E       File "<frozen zipimport>", line 259, in load_module
E       File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/exceptions/base.py", line 23, in <module>
E       File "<frozen importlib._bootstrap>", line 991, in _find_and_load
E       File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
E       File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
E       File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
E       File "<frozen zipimport>", line 259, in load_module
E       File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/utils.py", line 40, in <module>
E       File "<frozen importlib._bootstrap>", line 991, in _find_and_load
E       File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
E       File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
E       File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
E       File "<frozen zipimport>", line 259, in load_module
E       File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/error_classes.py", line 26, in <module>
E     AttributeError: module 'importlib.resources' has no attribute 'files'
E   PYTHONPATH was:
E     /Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip:/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/py4j-0.10.9.9-src.zip:/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/jars/spark-core_2.13-4.1.1.jar
E   org.apache.spark.SparkException: EOFException occurred while reading the port number from pyspark.daemon's stdout.
E   	at org.apache.spark.errors.SparkCoreErrors$.eofExceptionWhileReadPortNumberError(SparkCoreErrors.scala:55)
E   	at org.apache.spark.api.python.PythonWorkerFactory.startDaemon(PythonWorkerFactory.scala:335)
E   	at org.apache.spark.api.python.PythonWorkerFactory.createThroughDaemon(PythonWorkerFactory.scala:188)
E   	at org.apache.spark.api.python.PythonWorkerFactory.create(PythonWorkerFactory.scala:152)
E   	at org.apache.spark.SparkEnv.createPythonWorker(SparkEnv.scala:158)
E   	at org.apache.spark.api.python.BasePythonRunner.compute(PythonRunner.scala:309)
E   	at org.apache.spark.api.python.PythonRDD.compute(PythonRDD.scala:72)
E   	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
E   	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
E   	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
E   	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
E   	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
E   	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
E   	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
E   	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
E   	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
E   	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
E   	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
E   	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
E   	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
E   	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
E   	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
E   	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
E   	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
E   	at org.apache.spark.scheduler.ShuffleMapTask.runTask(ShuffleMapTask.scala:107)
E   	at org.apache.spark.scheduler.ShuffleMapTask.runTask(ShuffleMapTask.scala:54)
E   	at org.apache.spark.TaskContext.runTaskWithListeners(TaskContext.scala:180)
E   	at org.apache.spark.scheduler.Task.run(Task.scala:147)
E   	at org.apache.spark.executor.Executor$TaskRunner.$anonfun$run$5(Executor.scala:716)
E   	at org.apache.spark.util.SparkErrorUtils.tryWithSafeFinally(SparkErrorUtils.scala:86)
E   	at org.apache.spark.util.SparkErrorUtils.tryWithSafeFinally$(SparkErrorUtils.scala:83)
E   	at org.apache.spark.util.Utils$.tryWithSafeFinally(Utils.scala:97)
E   	at org.apache.spark.executor.Executor$TaskRunner.run(Executor.scala:719)
E   	at java.base/java.util.concurrent.ThreadPoolExecutor.runWorker(ThreadPoolExecutor.java:1136)
E   	at java.base/java.util.concurrent.ThreadPoolExecutor$Worker.run(ThreadPoolExecutor.java:635)
E   	at java.base/java.lang.Thread.run(Thread.java:833)
----------------------------- Captured stderr call -----------------------------
26/05/09 12:26:09 WARN TaskSetManager: Lost task 0.0 in stage 1.0 (TID 2) (192.168.1.101 executor driver): TaskKilled (Stage cancelled: Job aborted due to stage failure: Task 1 in stage 1.0 failed 1 times, most recent failure: Lost task 1.0 in stage 1.0 (TID 3) (192.168.1.101 executor driver): org.apache.spark.SparkException: 
Error from python worker:
  Traceback (most recent call last):
    File "/Library/Frameworks/Python.framework/Versions/3.8/lib/python3.8/runpy.py", line 185, in _run_module_as_main
      mod_name, mod_spec, code = _get_module_details(mod_name, _Error)
    File "/Library/Frameworks/Python.framework/Versions/3.8/lib/python3.8/runpy.py", line 111, in _get_module_details
      __import__(pkg_name)
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/__init__.py", line 53, in <module>
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/util.py", line 35, in <module>
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/__init__.py", line 21, in <module>
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/exceptions/base.py", line 23, in <module>
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/utils.py", line 40, in <module>
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/error_classes.py", line 26, in <module>
  AttributeError: module 'importlib.resources' has no attribute 'files'
PYTHONPATH was:
  /Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip:/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/py4j-0.10.9.9-src.zip:/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/jars/spark-core_2.13-4.1.1.jar
org.apache.spark.SparkException: EOFException occurred while reading the port number from pyspark.daemon's stdout and terminated with code: 1..
	at org.apache.spark.errors.SparkCoreErrors$.eofExceptionWhileReadPortNumberError(SparkCoreErrors.scala:55)
	at org.apache.spark.api.python.PythonWorkerFactory.startDaemon(PythonWorkerFactory.scala:339)
	at org.apache.spark.api.python.PythonWorkerFactory.createThroughDaemon(PythonWorkerFactory.scala:188)
	at org.apache.spark.api.python.PythonWorkerFactory.create(PythonWorkerFactory.scala:152)
	at org.apache.spark.SparkEnv.createPythonWorker(SparkEnv.scala:158)
	at org.apache.spark.api.python.BasePythonRunner.compute(PythonRunner.scala:309)
	at org.apache.spark.api.python.PythonRDD.compute(PythonRDD.scala:72)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.scheduler.ShuffleMapTask.runTask(ShuffleMapTask.scala:107)
	at org.apache.spark.scheduler.ShuffleMapTask.runTask(ShuffleMapTask.scala:54)
	at org.apache.spark.TaskContext.runTaskWithListeners(TaskContext.scala:180)
	at org.apache.spark.scheduler.Task.run(Task.scala:147)
	at org.apache.spark.executor.Executor$TaskRunner.$anonfun$run$5(Executor.scala:716)
	at org.apache.spark.util.SparkErrorUtils.tryWithSafeFinally(SparkErrorUtils.scala:86)
	at org.apache.spark.util.SparkErrorUtils.tryWithSafeFinally$(SparkErrorUtils.scala:83)
	at org.apache.spark.util.Utils$.tryWithSafeFinally(Utils.scala:97)
	at org.apache.spark.executor.Executor$TaskRunner.run(Executor.scala:719)
	at java.base/java.util.concurrent.ThreadPoolExecutor.runWorker(ThreadPoolExecutor.java:1136)
	at java.base/java.util.concurrent.ThreadPoolExecutor$Worker.run(ThreadPoolExecutor.java:635)
	at java.base/java.lang.Thread.run(Thread.java:833)

Driver stacktrace:)
26/05/09 12:26:09 ERROR Executor: Exception in task 0.0 in stage 2.0 (TID 4)
org.apache.spark.SparkException: 
Error from python worker:
  Traceback (most recent call last):
    File "/Library/Frameworks/Python.framework/Versions/3.8/lib/python3.8/runpy.py", line 185, in _run_module_as_main
      mod_name, mod_spec, code = _get_module_details(mod_name, _Error)
    File "/Library/Frameworks/Python.framework/Versions/3.8/lib/python3.8/runpy.py", line 111, in _get_module_details
      __import__(pkg_name)
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/__init__.py", line 53, in <module>
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/util.py", line 35, in <module>
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/__init__.py", line 21, in <module>
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/exceptions/base.py", line 23, in <module>
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/utils.py", line 40, in <module>
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/error_classes.py", line 26, in <module>
  AttributeError: module 'importlib.resources' has no attribute 'files'
PYTHONPATH was:
  /Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip:/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/py4j-0.10.9.9-src.zip:/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/jars/spark-core_2.13-4.1.1.jar
org.apache.spark.SparkException: EOFException occurred while reading the port number from pyspark.daemon's stdout.
	at org.apache.spark.errors.SparkCoreErrors$.eofExceptionWhileReadPortNumberError(SparkCoreErrors.scala:55)
	at org.apache.spark.api.python.PythonWorkerFactory.startDaemon(PythonWorkerFactory.scala:335)
	at org.apache.spark.api.python.PythonWorkerFactory.createThroughDaemon(PythonWorkerFactory.scala:188)
	at org.apache.spark.api.python.PythonWorkerFactory.create(PythonWorkerFactory.scala:152)
	at org.apache.spark.SparkEnv.createPythonWorker(SparkEnv.scala:158)
	at org.apache.spark.api.python.BasePythonRunner.compute(PythonRunner.scala:309)
	at org.apache.spark.api.python.PythonRDD.compute(PythonRDD.scala:72)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.scheduler.ShuffleMapTask.runTask(ShuffleMapTask.scala:107)
	at org.apache.spark.scheduler.ShuffleMapTask.runTask(ShuffleMapTask.scala:54)
	at org.apache.spark.TaskContext.runTaskWithListeners(TaskContext.scala:180)
	at org.apache.spark.scheduler.Task.run(Task.scala:147)
	at org.apache.spark.executor.Executor$TaskRunner.$anonfun$run$5(Executor.scala:716)
	at org.apache.spark.util.SparkErrorUtils.tryWithSafeFinally(SparkErrorUtils.scala:86)
	at org.apache.spark.util.SparkErrorUtils.tryWithSafeFinally$(SparkErrorUtils.scala:83)
	at org.apache.spark.util.Utils$.tryWithSafeFinally(Utils.scala:97)
	at org.apache.spark.executor.Executor$TaskRunner.run(Executor.scala:719)
	at java.base/java.util.concurrent.ThreadPoolExecutor.runWorker(ThreadPoolExecutor.java:1136)
	at java.base/java.util.concurrent.ThreadPoolExecutor$Worker.run(ThreadPoolExecutor.java:635)
	at java.base/java.lang.Thread.run(Thread.java:833)
26/05/09 12:26:09 WARN TaskSetManager: Lost task 0.0 in stage 2.0 (TID 4) (192.168.1.101 executor driver): org.apache.spark.SparkException: 
Error from python worker:
  Traceback (most recent call last):
    File "/Library/Frameworks/Python.framework/Versions/3.8/lib/python3.8/runpy.py", line 185, in _run_module_as_main
      mod_name, mod_spec, code = _get_module_details(mod_name, _Error)
    File "/Library/Frameworks/Python.framework/Versions/3.8/lib/python3.8/runpy.py", line 111, in _get_module_details
      __import__(pkg_name)
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/__init__.py", line 53, in <module>
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/util.py", line 35, in <module>
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/__init__.py", line 21, in <module>
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/exceptions/base.py", line 23, in <module>
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/utils.py", line 40, in <module>
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/error_classes.py", line 26, in <module>
  AttributeError: module 'importlib.resources' has no attribute 'files'
PYTHONPATH was:
  /Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip:/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/py4j-0.10.9.9-src.zip:/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/jars/spark-core_2.13-4.1.1.jar
org.apache.spark.SparkException: EOFException occurred while reading the port number from pyspark.daemon's stdout.
	at org.apache.spark.errors.SparkCoreErrors$.eofExceptionWhileReadPortNumberError(SparkCoreErrors.scala:55)
	at org.apache.spark.api.python.PythonWorkerFactory.startDaemon(PythonWorkerFactory.scala:335)
	at org.apache.spark.api.python.PythonWorkerFactory.createThroughDaemon(PythonWorkerFactory.scala:188)
	at org.apache.spark.api.python.PythonWorkerFactory.create(PythonWorkerFactory.scala:152)
	at org.apache.spark.SparkEnv.createPythonWorker(SparkEnv.scala:158)
	at org.apache.spark.api.python.BasePythonRunner.compute(PythonRunner.scala:309)
	at org.apache.spark.api.python.PythonRDD.compute(PythonRDD.scala:72)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.scheduler.ShuffleMapTask.runTask(ShuffleMapTask.scala:107)
	at org.apache.spark.scheduler.ShuffleMapTask.runTask(ShuffleMapTask.scala:54)
	at org.apache.spark.TaskContext.runTaskWithListeners(TaskContext.scala:180)
	at org.apache.spark.scheduler.Task.run(Task.scala:147)
	at org.apache.spark.executor.Executor$TaskRunner.$anonfun$run$5(Executor.scala:716)
	at org.apache.spark.util.SparkErrorUtils.tryWithSafeFinally(SparkErrorUtils.scala:86)
	at org.apache.spark.util.SparkErrorUtils.tryWithSafeFinally$(SparkErrorUtils.scala:83)
	at org.apache.spark.util.Utils$.tryWithSafeFinally(Utils.scala:97)
	at org.apache.spark.executor.Executor$TaskRunner.run(Executor.scala:719)
	at java.base/java.util.concurrent.ThreadPoolExecutor.runWorker(ThreadPoolExecutor.java:1136)
	at java.base/java.util.concurrent.ThreadPoolExecutor$Worker.run(ThreadPoolExecutor.java:635)
	at java.base/java.lang.Thread.run(Thread.java:833)

26/05/09 12:26:09 ERROR TaskSetManager: Task 0 in stage 2.0 failed 1 times; aborting job
--------------------------- Captured stderr teardown ---------------------------
26/05/09 12:26:10 WARN TaskSetManager: Lost task 1.0 in stage 2.0 (TID 5) (192.168.1.101 executor driver): TaskKilled (Stage cancelled: Job aborted due to stage failure: Task 0 in stage 2.0 failed 1 times, most recent failure: Lost task 0.0 in stage 2.0 (TID 4) (192.168.1.101 executor driver): org.apache.spark.SparkException: 
Error from python worker:
  Traceback (most recent call last):
    File "/Library/Frameworks/Python.framework/Versions/3.8/lib/python3.8/runpy.py", line 185, in _run_module_as_main
      mod_name, mod_spec, code = _get_module_details(mod_name, _Error)
    File "/Library/Frameworks/Python.framework/Versions/3.8/lib/python3.8/runpy.py", line 111, in _get_module_details
      __import__(pkg_name)
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/__init__.py", line 53, in <module>
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/util.py", line 35, in <module>
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/__init__.py", line 21, in <module>
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/exceptions/base.py", line 23, in <module>
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/utils.py", line 40, in <module>
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/error_classes.py", line 26, in <module>
  AttributeError: module 'importlib.resources' has no attribute 'files'
PYTHONPATH was:
  /Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip:/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/py4j-0.10.9.9-src.zip:/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/jars/spark-core_2.13-4.1.1.jar
org.apache.spark.SparkException: EOFException occurred while reading the port number from pyspark.daemon's stdout.
	at org.apache.spark.errors.SparkCoreErrors$.eofExceptionWhileReadPortNumberError(SparkCoreErrors.scala:55)
	at org.apache.spark.api.python.PythonWorkerFactory.startDaemon(PythonWorkerFactory.scala:335)
	at org.apache.spark.api.python.PythonWorkerFactory.createThroughDaemon(PythonWorkerFactory.scala:188)
	at org.apache.spark.api.python.PythonWorkerFactory.create(PythonWorkerFactory.scala:152)
	at org.apache.spark.SparkEnv.createPythonWorker(SparkEnv.scala:158)
	at org.apache.spark.api.python.BasePythonRunner.compute(PythonRunner.scala:309)
	at org.apache.spark.api.python.PythonRDD.compute(PythonRDD.scala:72)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.scheduler.ShuffleMapTask.runTask(ShuffleMapTask.scala:107)
	at org.apache.spark.scheduler.ShuffleMapTask.runTask(ShuffleMapTask.scala:54)
	at org.apache.spark.TaskContext.runTaskWithListeners(TaskContext.scala:180)
	at org.apache.spark.scheduler.Task.run(Task.scala:147)
	at org.apache.spark.executor.Executor$TaskRunner.$anonfun$run$5(Executor.scala:716)
	at org.apache.spark.util.SparkErrorUtils.tryWithSafeFinally(SparkErrorUtils.scala:86)
	at org.apache.spark.util.SparkErrorUtils.tryWithSafeFinally$(SparkErrorUtils.scala:83)
	at org.apache.spark.util.Utils$.tryWithSafeFinally(Utils.scala:97)
	at org.apache.spark.executor.Executor$TaskRunner.run(Executor.scala:719)
	at java.base/java.util.concurrent.ThreadPoolExecutor.runWorker(ThreadPoolExecutor.java:1136)
	at java.base/java.util.concurrent.ThreadPoolExecutor$Worker.run(ThreadPoolExecutor.java:635)
	at java.base/java.lang.Thread.run(Thread.java:833)

Driver stacktrace:)
________________________ test_enrich_adds_jieba_columns ________________________
tests/test_spark_pipeline.py:180: in test_enrich_adds_jieba_columns
    row = enriched.first()
          ^^^^^^^^^^^^^^^^
.venv/lib/python3.13/site-packages/pyspark/sql/classic/dataframe.py:964: in first
    return self.head()
           ^^^^^^^^^^^
.venv/lib/python3.13/site-packages/pyspark/sql/classic/dataframe.py:959: in head
    rs = self.head(1)
         ^^^^^^^^^^^^
.venv/lib/python3.13/site-packages/pyspark/sql/classic/dataframe.py:961: in head
    return self.take(n)
           ^^^^^^^^^^^^
.venv/lib/python3.13/site-packages/pyspark/sql/classic/dataframe.py:460: in take
    return self.limit(num).collect()
           ^^^^^^^^^^^^^^^^^^^^^^^^^
.venv/lib/python3.13/site-packages/pyspark/sql/classic/dataframe.py:443: in collect
    sock_info = self._jdf.collectToPython()
                ^^^^^^^^^^^^^^^^^^^^^^^^^^^
.venv/lib/python3.13/site-packages/py4j/java_gateway.py:1362: in __call__
    return_value = get_return_value(
.venv/lib/python3.13/site-packages/pyspark/errors/exceptions/captured.py:263: in deco
    return f(*a, **kw)
           ^^^^^^^^^^^
.venv/lib/python3.13/site-packages/py4j/protocol.py:327: in get_return_value
    raise Py4JJavaError(
E   py4j.protocol.Py4JJavaError: An error occurred while calling o364.collectToPython.
E   : org.apache.spark.SparkException: Job aborted due to stage failure: Task 0 in stage 3.0 failed 1 times, most recent failure: Lost task 0.0 in stage 3.0 (TID 6) (192.168.1.101 executor driver): org.apache.spark.SparkException: 
E   Error from python worker:
E     Traceback (most recent call last):
E       File "/Library/Frameworks/Python.framework/Versions/3.8/lib/python3.8/runpy.py", line 185, in _run_module_as_main
E         mod_name, mod_spec, code = _get_module_details(mod_name, _Error)
E       File "/Library/Frameworks/Python.framework/Versions/3.8/lib/python3.8/runpy.py", line 111, in _get_module_details
E         __import__(pkg_name)
E       File "<frozen importlib._bootstrap>", line 991, in _find_and_load
E       File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
E       File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
E       File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
E       File "<frozen zipimport>", line 259, in load_module
E       File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/__init__.py", line 53, in <module>
E       File "<frozen importlib._bootstrap>", line 991, in _find_and_load
E       File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
E       File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
E       File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
E       File "<frozen zipimport>", line 259, in load_module
E       File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/util.py", line 35, in <module>
E       File "<frozen importlib._bootstrap>", line 991, in _find_and_load
E       File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
E       File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
E       File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
E       File "<frozen zipimport>", line 259, in load_module
E       File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/__init__.py", line 21, in <module>
E       File "<frozen importlib._bootstrap>", line 991, in _find_and_load
E       File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
E       File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
E       File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
E       File "<frozen zipimport>", line 259, in load_module
E       File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/exceptions/base.py", line 23, in <module>
E       File "<frozen importlib._bootstrap>", line 991, in _find_and_load
E       File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
E       File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
E       File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
E       File "<frozen zipimport>", line 259, in load_module
E       File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/utils.py", line 40, in <module>
E       File "<frozen importlib._bootstrap>", line 991, in _find_and_load
E       File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
E       File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
E       File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
E       File "<frozen zipimport>", line 259, in load_module
E       File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/error_classes.py", line 26, in <module>
E     AttributeError: module 'importlib.resources' has no attribute 'files'
E   PYTHONPATH was:
E     /Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip:/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/py4j-0.10.9.9-src.zip:/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/jars/spark-core_2.13-4.1.1.jar
E   org.apache.spark.SparkException: EOFException occurred while reading the port number from pyspark.daemon's stdout and terminated with code: 1..
E   	at org.apache.spark.errors.SparkCoreErrors$.eofExceptionWhileReadPortNumberError(SparkCoreErrors.scala:55)
E   	at org.apache.spark.api.python.PythonWorkerFactory.startDaemon(PythonWorkerFactory.scala:339)
E   	at org.apache.spark.api.python.PythonWorkerFactory.createThroughDaemon(PythonWorkerFactory.scala:188)
E   	at org.apache.spark.api.python.PythonWorkerFactory.create(PythonWorkerFactory.scala:152)
E   	at org.apache.spark.SparkEnv.createPythonWorker(SparkEnv.scala:158)
E   	at org.apache.spark.api.python.BasePythonRunner.compute(PythonRunner.scala:309)
E   	at org.apache.spark.api.python.PythonRDD.compute(PythonRDD.scala:72)
E   	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
E   	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
E   	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
E   	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
E   	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
E   	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
E   	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
E   	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
E   	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
E   	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
E   	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
E   	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
E   	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
E   	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
E   	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
E   	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
E   	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
E   	at org.apache.spark.scheduler.ShuffleMapTask.runTask(ShuffleMapTask.scala:107)
E   	at org.apache.spark.scheduler.ShuffleMapTask.runTask(ShuffleMapTask.scala:54)
E   	at org.apache.spark.TaskContext.runTaskWithListeners(TaskContext.scala:180)
E   	at org.apache.spark.scheduler.Task.run(Task.scala:147)
E   	at org.apache.spark.executor.Executor$TaskRunner.$anonfun$run$5(Executor.scala:716)
E   	at org.apache.spark.util.SparkErrorUtils.tryWithSafeFinally(SparkErrorUtils.scala:86)
E   	at org.apache.spark.util.SparkErrorUtils.tryWithSafeFinally$(SparkErrorUtils.scala:83)
E   	at org.apache.spark.util.Utils$.tryWithSafeFinally(Utils.scala:97)
E   	at org.apache.spark.executor.Executor$TaskRunner.run(Executor.scala:719)
E   	at java.base/java.util.concurrent.ThreadPoolExecutor.runWorker(ThreadPoolExecutor.java:1136)
E   	at java.base/java.util.concurrent.ThreadPoolExecutor$Worker.run(ThreadPoolExecutor.java:635)
E   	at java.base/java.lang.Thread.run(Thread.java:833)
E   
E   Driver stacktrace:
E   	at org.apache.spark.scheduler.DAGScheduler.$anonfun$abortStage$3(DAGScheduler.scala:3122)
E   	at scala.Option.getOrElse(Option.scala:201)
E   	at org.apache.spark.scheduler.DAGScheduler.$anonfun$abortStage$2(DAGScheduler.scala:3122)
E   	at org.apache.spark.scheduler.DAGScheduler.$anonfun$abortStage$2$adapted(DAGScheduler.scala:3114)
E   	at scala.collection.immutable.List.foreach(List.scala:323)
E   	at org.apache.spark.scheduler.DAGScheduler.abortStage(DAGScheduler.scala:3114)
E   	at org.apache.spark.scheduler.DAGScheduler.$anonfun$handleTaskSetFailed$1(DAGScheduler.scala:1303)
E   	at org.apache.spark.scheduler.DAGScheduler.$anonfun$handleTaskSetFailed$1$adapted(DAGScheduler.scala:1303)
E   	at scala.Option.foreach(Option.scala:437)
E   	at org.apache.spark.scheduler.DAGScheduler.handleTaskSetFailed(DAGScheduler.scala:1303)
E   	at org.apache.spark.scheduler.DAGSchedulerEventProcessLoop.doOnReceive(DAGScheduler.scala:3397)
E   	at org.apache.spark.scheduler.DAGSchedulerEventProcessLoop.onReceive(DAGScheduler.scala:3328)
E   	at org.apache.spark.scheduler.DAGSchedulerEventProcessLoop.onReceive(DAGScheduler.scala:3317)
E   	at org.apache.spark.util.EventLoop$$anon$1.run(EventLoop.scala:50)
E   Caused by: org.apache.spark.SparkException: 
E   Error from python worker:
E     Traceback (most recent call last):
E       File "/Library/Frameworks/Python.framework/Versions/3.8/lib/python3.8/runpy.py", line 185, in _run_module_as_main
E         mod_name, mod_spec, code = _get_module_details(mod_name, _Error)
E       File "/Library/Frameworks/Python.framework/Versions/3.8/lib/python3.8/runpy.py", line 111, in _get_module_details
E         __import__(pkg_name)
E       File "<frozen importlib._bootstrap>", line 991, in _find_and_load
E       File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
E       File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
E       File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
E       File "<frozen zipimport>", line 259, in load_module
E       File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/__init__.py", line 53, in <module>
E       File "<frozen importlib._bootstrap>", line 991, in _find_and_load
E       File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
E       File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
E       File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
E       File "<frozen zipimport>", line 259, in load_module
E       File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/util.py", line 35, in <module>
E       File "<frozen importlib._bootstrap>", line 991, in _find_and_load
E       File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
E       File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
E       File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
E       File "<frozen zipimport>", line 259, in load_module
E       File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/__init__.py", line 21, in <module>
E       File "<frozen importlib._bootstrap>", line 991, in _find_and_load
E       File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
E       File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
E       File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
E       File "<frozen zipimport>", line 259, in load_module
E       File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/exceptions/base.py", line 23, in <module>
E       File "<frozen importlib._bootstrap>", line 991, in _find_and_load
E       File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
E       File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
E       File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
E       File "<frozen zipimport>", line 259, in load_module
E       File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/utils.py", line 40, in <module>
E       File "<frozen importlib._bootstrap>", line 991, in _find_and_load
E       File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
E       File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
E       File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
E       File "<frozen zipimport>", line 259, in load_module
E       File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/error_classes.py", line 26, in <module>
E     AttributeError: module 'importlib.resources' has no attribute 'files'
E   PYTHONPATH was:
E     /Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip:/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/py4j-0.10.9.9-src.zip:/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/jars/spark-core_2.13-4.1.1.jar
E   org.apache.spark.SparkException: EOFException occurred while reading the port number from pyspark.daemon's stdout and terminated with code: 1..
E   	at org.apache.spark.errors.SparkCoreErrors$.eofExceptionWhileReadPortNumberError(SparkCoreErrors.scala:55)
E   	at org.apache.spark.api.python.PythonWorkerFactory.startDaemon(PythonWorkerFactory.scala:339)
E   	at org.apache.spark.api.python.PythonWorkerFactory.createThroughDaemon(PythonWorkerFactory.scala:188)
E   	at org.apache.spark.api.python.PythonWorkerFactory.create(PythonWorkerFactory.scala:152)
E   	at org.apache.spark.SparkEnv.createPythonWorker(SparkEnv.scala:158)
E   	at org.apache.spark.api.python.BasePythonRunner.compute(PythonRunner.scala:309)
E   	at org.apache.spark.api.python.PythonRDD.compute(PythonRDD.scala:72)
E   	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
E   	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
E   	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
E   	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
E   	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
E   	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
E   	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
E   	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
E   	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
E   	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
E   	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
E   	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
E   	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
E   	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
E   	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
E   	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
E   	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
E   	at org.apache.spark.scheduler.ShuffleMapTask.runTask(ShuffleMapTask.scala:107)
E   	at org.apache.spark.scheduler.ShuffleMapTask.runTask(ShuffleMapTask.scala:54)
E   	at org.apache.spark.TaskContext.runTaskWithListeners(TaskContext.scala:180)
E   	at org.apache.spark.scheduler.Task.run(Task.scala:147)
E   	at org.apache.spark.executor.Executor$TaskRunner.$anonfun$run$5(Executor.scala:716)
E   	at org.apache.spark.util.SparkErrorUtils.tryWithSafeFinally(SparkErrorUtils.scala:86)
E   	at org.apache.spark.util.SparkErrorUtils.tryWithSafeFinally$(SparkErrorUtils.scala:83)
E   	at org.apache.spark.util.Utils$.tryWithSafeFinally(Utils.scala:97)
E   	at org.apache.spark.executor.Executor$TaskRunner.run(Executor.scala:719)
E   	at java.base/java.util.concurrent.ThreadPoolExecutor.runWorker(ThreadPoolExecutor.java:1136)
E   	at java.base/java.util.concurrent.ThreadPoolExecutor$Worker.run(ThreadPoolExecutor.java:635)
E   	at java.base/java.lang.Thread.run(Thread.java:833)
----------------------------- Captured stderr call -----------------------------
26/05/09 12:26:10 WARN SparkContext: The path /Users/andrew/Desktop/andrew/Data_engineer/line_bot/finetune/spark_pipeline.py has been added already. Overwriting of added paths is not supported in the current version.
26/05/09 12:26:10 WARN SparkContext: The path /Users/andrew/Desktop/andrew/Data_engineer/line_bot/finetune/spark_pipeline.py has been added already. Overwriting of added paths is not supported in the current version.
26/05/09 12:26:11 ERROR Executor: Exception in task 0.0 in stage 3.0 (TID 6)
org.apache.spark.SparkException: 
Error from python worker:
  Traceback (most recent call last):
    File "/Library/Frameworks/Python.framework/Versions/3.8/lib/python3.8/runpy.py", line 185, in _run_module_as_main
      mod_name, mod_spec, code = _get_module_details(mod_name, _Error)
    File "/Library/Frameworks/Python.framework/Versions/3.8/lib/python3.8/runpy.py", line 111, in _get_module_details
      __import__(pkg_name)
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/__init__.py", line 53, in <module>
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/util.py", line 35, in <module>
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/__init__.py", line 21, in <module>
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/exceptions/base.py", line 23, in <module>
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/utils.py", line 40, in <module>
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/error_classes.py", line 26, in <module>
  AttributeError: module 'importlib.resources' has no attribute 'files'
PYTHONPATH was:
  /Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip:/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/py4j-0.10.9.9-src.zip:/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/jars/spark-core_2.13-4.1.1.jar
org.apache.spark.SparkException: EOFException occurred while reading the port number from pyspark.daemon's stdout and terminated with code: 1..
	at org.apache.spark.errors.SparkCoreErrors$.eofExceptionWhileReadPortNumberError(SparkCoreErrors.scala:55)
	at org.apache.spark.api.python.PythonWorkerFactory.startDaemon(PythonWorkerFactory.scala:339)
	at org.apache.spark.api.python.PythonWorkerFactory.createThroughDaemon(PythonWorkerFactory.scala:188)
	at org.apache.spark.api.python.PythonWorkerFactory.create(PythonWorkerFactory.scala:152)
	at org.apache.spark.SparkEnv.createPythonWorker(SparkEnv.scala:158)
	at org.apache.spark.api.python.BasePythonRunner.compute(PythonRunner.scala:309)
	at org.apache.spark.api.python.PythonRDD.compute(PythonRDD.scala:72)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.scheduler.ShuffleMapTask.runTask(ShuffleMapTask.scala:107)
	at org.apache.spark.scheduler.ShuffleMapTask.runTask(ShuffleMapTask.scala:54)
	at org.apache.spark.TaskContext.runTaskWithListeners(TaskContext.scala:180)
	at org.apache.spark.scheduler.Task.run(Task.scala:147)
	at org.apache.spark.executor.Executor$TaskRunner.$anonfun$run$5(Executor.scala:716)
	at org.apache.spark.util.SparkErrorUtils.tryWithSafeFinally(SparkErrorUtils.scala:86)
	at org.apache.spark.util.SparkErrorUtils.tryWithSafeFinally$(SparkErrorUtils.scala:83)
	at org.apache.spark.util.Utils$.tryWithSafeFinally(Utils.scala:97)
	at org.apache.spark.executor.Executor$TaskRunner.run(Executor.scala:719)
	at java.base/java.util.concurrent.ThreadPoolExecutor.runWorker(ThreadPoolExecutor.java:1136)
	at java.base/java.util.concurrent.ThreadPoolExecutor$Worker.run(ThreadPoolExecutor.java:635)
	at java.base/java.lang.Thread.run(Thread.java:833)
26/05/09 12:26:11 WARN TaskSetManager: Lost task 0.0 in stage 3.0 (TID 6) (192.168.1.101 executor driver): org.apache.spark.SparkException: 
Error from python worker:
  Traceback (most recent call last):
    File "/Library/Frameworks/Python.framework/Versions/3.8/lib/python3.8/runpy.py", line 185, in _run_module_as_main
      mod_name, mod_spec, code = _get_module_details(mod_name, _Error)
    File "/Library/Frameworks/Python.framework/Versions/3.8/lib/python3.8/runpy.py", line 111, in _get_module_details
      __import__(pkg_name)
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/__init__.py", line 53, in <module>
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/util.py", line 35, in <module>
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/__init__.py", line 21, in <module>
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/exceptions/base.py", line 23, in <module>
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/utils.py", line 40, in <module>
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/error_classes.py", line 26, in <module>
  AttributeError: module 'importlib.resources' has no attribute 'files'
PYTHONPATH was:
  /Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip:/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/py4j-0.10.9.9-src.zip:/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/jars/spark-core_2.13-4.1.1.jar
org.apache.spark.SparkException: EOFException occurred while reading the port number from pyspark.daemon's stdout and terminated with code: 1..
	at org.apache.spark.errors.SparkCoreErrors$.eofExceptionWhileReadPortNumberError(SparkCoreErrors.scala:55)
	at org.apache.spark.api.python.PythonWorkerFactory.startDaemon(PythonWorkerFactory.scala:339)
	at org.apache.spark.api.python.PythonWorkerFactory.createThroughDaemon(PythonWorkerFactory.scala:188)
	at org.apache.spark.api.python.PythonWorkerFactory.create(PythonWorkerFactory.scala:152)
	at org.apache.spark.SparkEnv.createPythonWorker(SparkEnv.scala:158)
	at org.apache.spark.api.python.BasePythonRunner.compute(PythonRunner.scala:309)
	at org.apache.spark.api.python.PythonRDD.compute(PythonRDD.scala:72)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.scheduler.ShuffleMapTask.runTask(ShuffleMapTask.scala:107)
	at org.apache.spark.scheduler.ShuffleMapTask.runTask(ShuffleMapTask.scala:54)
	at org.apache.spark.TaskContext.runTaskWithListeners(TaskContext.scala:180)
	at org.apache.spark.scheduler.Task.run(Task.scala:147)
	at org.apache.spark.executor.Executor$TaskRunner.$anonfun$run$5(Executor.scala:716)
	at org.apache.spark.util.SparkErrorUtils.tryWithSafeFinally(SparkErrorUtils.scala:86)
	at org.apache.spark.util.SparkErrorUtils.tryWithSafeFinally$(SparkErrorUtils.scala:83)
	at org.apache.spark.util.Utils$.tryWithSafeFinally(Utils.scala:97)
	at org.apache.spark.executor.Executor$TaskRunner.run(Executor.scala:719)
	at java.base/java.util.concurrent.ThreadPoolExecutor.runWorker(ThreadPoolExecutor.java:1136)
	at java.base/java.util.concurrent.ThreadPoolExecutor$Worker.run(ThreadPoolExecutor.java:635)
	at java.base/java.lang.Thread.run(Thread.java:833)

26/05/09 12:26:11 ERROR TaskSetManager: Task 0 in stage 3.0 failed 1 times; aborting job
____________________________ test_dedup_hash_stable ____________________________
tests/test_spark_pipeline.py:197: in test_dedup_hash_stable
    assert cleaned.count() == 1
           ^^^^^^^^^^^^^^^
.venv/lib/python3.13/site-packages/pyspark/sql/classic/dataframe.py:439: in count
    return int(self._jdf.count())
               ^^^^^^^^^^^^^^^^^
.venv/lib/python3.13/site-packages/py4j/java_gateway.py:1362: in __call__
    return_value = get_return_value(
.venv/lib/python3.13/site-packages/pyspark/errors/exceptions/captured.py:263: in deco
    return f(*a, **kw)
           ^^^^^^^^^^^
.venv/lib/python3.13/site-packages/py4j/protocol.py:327: in get_return_value
    raise Py4JJavaError(
E   py4j.protocol.Py4JJavaError: An error occurred while calling o455.count.
E   : org.apache.spark.SparkException: Job aborted due to stage failure: Task 1 in stage 4.0 failed 1 times, most recent failure: Lost task 1.0 in stage 4.0 (TID 9) (192.168.1.101 executor driver): org.apache.spark.SparkException: 
E   Error from python worker:
E     Traceback (most recent call last):
E       File "/Library/Frameworks/Python.framework/Versions/3.8/lib/python3.8/runpy.py", line 185, in _run_module_as_main
E         mod_name, mod_spec, code = _get_module_details(mod_name, _Error)
E       File "/Library/Frameworks/Python.framework/Versions/3.8/lib/python3.8/runpy.py", line 111, in _get_module_details
E         __import__(pkg_name)
E       File "<frozen importlib._bootstrap>", line 991, in _find_and_load
E       File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
E       File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
E       File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
E       File "<frozen zipimport>", line 259, in load_module
E       File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/__init__.py", line 53, in <module>
E       File "<frozen importlib._bootstrap>", line 991, in _find_and_load
E       File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
E       File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
E       File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
E       File "<frozen zipimport>", line 259, in load_module
E       File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/util.py", line 35, in <module>
E       File "<frozen importlib._bootstrap>", line 991, in _find_and_load
E       File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
E       File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
E       File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
E       File "<frozen zipimport>", line 259, in load_module
E       File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/__init__.py", line 21, in <module>
E       File "<frozen importlib._bootstrap>", line 991, in _find_and_load
E       File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
E       File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
E       File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
E       File "<frozen zipimport>", line 259, in load_module
E       File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/exceptions/base.py", line 23, in <module>
E       File "<frozen importlib._bootstrap>", line 991, in _find_and_load
E       File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
E       File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
E       File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
E       File "<frozen zipimport>", line 259, in load_module
E       File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/utils.py", line 40, in <module>
E       File "<frozen importlib._bootstrap>", line 991, in _find_and_load
E       File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
E       File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
E       File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
E       File "<frozen zipimport>", line 259, in load_module
E       File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/error_classes.py", line 26, in <module>
E     AttributeError: module 'importlib.resources' has no attribute 'files'
E   PYTHONPATH was:
E     /Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip:/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/py4j-0.10.9.9-src.zip:/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/jars/spark-core_2.13-4.1.1.jar
E   org.apache.spark.SparkException: EOFException occurred while reading the port number from pyspark.daemon's stdout and terminated with code: 1..
E   	at org.apache.spark.errors.SparkCoreErrors$.eofExceptionWhileReadPortNumberError(SparkCoreErrors.scala:55)
E   	at org.apache.spark.api.python.PythonWorkerFactory.startDaemon(PythonWorkerFactory.scala:339)
E   	at org.apache.spark.api.python.PythonWorkerFactory.createThroughDaemon(PythonWorkerFactory.scala:188)
E   	at org.apache.spark.api.python.PythonWorkerFactory.create(PythonWorkerFactory.scala:152)
E   	at org.apache.spark.SparkEnv.createPythonWorker(SparkEnv.scala:158)
E   	at org.apache.spark.api.python.BasePythonRunner.compute(PythonRunner.scala:309)
E   	at org.apache.spark.api.python.PythonRDD.compute(PythonRDD.scala:72)
E   	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
E   	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
E   	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
E   	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
E   	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
E   	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
E   	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
E   	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
E   	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
E   	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
E   	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
E   	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
E   	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
E   	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
E   	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
E   	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
E   	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
E   	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
E   	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
E   	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
E   	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
E   	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
E   	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
E   	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
E   	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
E   	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
E   	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
E   	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
E   	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
E   	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
E   	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
E   	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
E   	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
E   	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
E   	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
E   	at org.apache.spark.scheduler.ShuffleMapTask.runTask(ShuffleMapTask.scala:107)
E   	at org.apache.spark.scheduler.ShuffleMapTask.runTask(ShuffleMapTask.scala:54)
E   	at org.apache.spark.TaskContext.runTaskWithListeners(TaskContext.scala:180)
E   	at org.apache.spark.scheduler.Task.run(Task.scala:147)
E   	at org.apache.spark.executor.Executor$TaskRunner.$anonfun$run$5(Executor.scala:716)
E   	at org.apache.spark.util.SparkErrorUtils.tryWithSafeFinally(SparkErrorUtils.scala:86)
E   	at org.apache.spark.util.SparkErrorUtils.tryWithSafeFinally$(SparkErrorUtils.scala:83)
E   	at org.apache.spark.util.Utils$.tryWithSafeFinally(Utils.scala:97)
E   	at org.apache.spark.executor.Executor$TaskRunner.run(Executor.scala:719)
E   	at java.base/java.util.concurrent.ThreadPoolExecutor.runWorker(ThreadPoolExecutor.java:1136)
E   	at java.base/java.util.concurrent.ThreadPoolExecutor$Worker.run(ThreadPoolExecutor.java:635)
E   	at java.base/java.lang.Thread.run(Thread.java:833)
E   
E   Driver stacktrace:
E   	at org.apache.spark.scheduler.DAGScheduler.$anonfun$abortStage$3(DAGScheduler.scala:3122)
E   	at scala.Option.getOrElse(Option.scala:201)
E   	at org.apache.spark.scheduler.DAGScheduler.$anonfun$abortStage$2(DAGScheduler.scala:3122)
E   	at org.apache.spark.scheduler.DAGScheduler.$anonfun$abortStage$2$adapted(DAGScheduler.scala:3114)
E   	at scala.collection.immutable.List.foreach(List.scala:323)
E   	at org.apache.spark.scheduler.DAGScheduler.abortStage(DAGScheduler.scala:3114)
E   	at org.apache.spark.scheduler.DAGScheduler.$anonfun$handleTaskSetFailed$1(DAGScheduler.scala:1303)
E   	at org.apache.spark.scheduler.DAGScheduler.$anonfun$handleTaskSetFailed$1$adapted(DAGScheduler.scala:1303)
E   	at scala.Option.foreach(Option.scala:437)
E   	at org.apache.spark.scheduler.DAGScheduler.handleTaskSetFailed(DAGScheduler.scala:1303)
E   	at org.apache.spark.scheduler.DAGSchedulerEventProcessLoop.doOnReceive(DAGScheduler.scala:3397)
E   	at org.apache.spark.scheduler.DAGSchedulerEventProcessLoop.onReceive(DAGScheduler.scala:3328)
E   	at org.apache.spark.scheduler.DAGSchedulerEventProcessLoop.onReceive(DAGScheduler.scala:3317)
E   	at org.apache.spark.util.EventLoop$$anon$1.run(EventLoop.scala:50)
E   Caused by: org.apache.spark.SparkException: 
E   Error from python worker:
E     Traceback (most recent call last):
E       File "/Library/Frameworks/Python.framework/Versions/3.8/lib/python3.8/runpy.py", line 185, in _run_module_as_main
E         mod_name, mod_spec, code = _get_module_details(mod_name, _Error)
E       File "/Library/Frameworks/Python.framework/Versions/3.8/lib/python3.8/runpy.py", line 111, in _get_module_details
E         __import__(pkg_name)
E       File "<frozen importlib._bootstrap>", line 991, in _find_and_load
E       File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
E       File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
E       File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
E       File "<frozen zipimport>", line 259, in load_module
E       File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/__init__.py", line 53, in <module>
E       File "<frozen importlib._bootstrap>", line 991, in _find_and_load
E       File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
E       File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
E       File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
E       File "<frozen zipimport>", line 259, in load_module
E       File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/util.py", line 35, in <module>
E       File "<frozen importlib._bootstrap>", line 991, in _find_and_load
E       File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
E       File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
E       File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
E       File "<frozen zipimport>", line 259, in load_module
E       File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/__init__.py", line 21, in <module>
E       File "<frozen importlib._bootstrap>", line 991, in _find_and_load
E       File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
E       File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
E       File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
E       File "<frozen zipimport>", line 259, in load_module
E       File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/exceptions/base.py", line 23, in <module>
E       File "<frozen importlib._bootstrap>", line 991, in _find_and_load
E       File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
E       File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
E       File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
E       File "<frozen zipimport>", line 259, in load_module
E       File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/utils.py", line 40, in <module>
E       File "<frozen importlib._bootstrap>", line 991, in _find_and_load
E       File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
E       File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
E       File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
E       File "<frozen zipimport>", line 259, in load_module
E       File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/error_classes.py", line 26, in <module>
E     AttributeError: module 'importlib.resources' has no attribute 'files'
E   PYTHONPATH was:
E     /Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip:/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/py4j-0.10.9.9-src.zip:/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/jars/spark-core_2.13-4.1.1.jar
E   org.apache.spark.SparkException: EOFException occurred while reading the port number from pyspark.daemon's stdout and terminated with code: 1..
E   	at org.apache.spark.errors.SparkCoreErrors$.eofExceptionWhileReadPortNumberError(SparkCoreErrors.scala:55)
E   	at org.apache.spark.api.python.PythonWorkerFactory.startDaemon(PythonWorkerFactory.scala:339)
E   	at org.apache.spark.api.python.PythonWorkerFactory.createThroughDaemon(PythonWorkerFactory.scala:188)
E   	at org.apache.spark.api.python.PythonWorkerFactory.create(PythonWorkerFactory.scala:152)
E   	at org.apache.spark.SparkEnv.createPythonWorker(SparkEnv.scala:158)
E   	at org.apache.spark.api.python.BasePythonRunner.compute(PythonRunner.scala:309)
E   	at org.apache.spark.api.python.PythonRDD.compute(PythonRDD.scala:72)
E   	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
E   	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
E   	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
E   	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
E   	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
E   	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
E   	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
E   	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
E   	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
E   	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
E   	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
E   	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
E   	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
E   	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
E   	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
E   	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
E   	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
E   	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
E   	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
E   	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
E   	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
E   	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
E   	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
E   	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
E   	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
E   	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
E   	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
E   	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
E   	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
E   	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
E   	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
E   	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
E   	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
E   	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
E   	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
E   	at org.apache.spark.scheduler.ShuffleMapTask.runTask(ShuffleMapTask.scala:107)
E   	at org.apache.spark.scheduler.ShuffleMapTask.runTask(ShuffleMapTask.scala:54)
E   	at org.apache.spark.TaskContext.runTaskWithListeners(TaskContext.scala:180)
E   	at org.apache.spark.scheduler.Task.run(Task.scala:147)
E   	at org.apache.spark.executor.Executor$TaskRunner.$anonfun$run$5(Executor.scala:716)
E   	at org.apache.spark.util.SparkErrorUtils.tryWithSafeFinally(SparkErrorUtils.scala:86)
E   	at org.apache.spark.util.SparkErrorUtils.tryWithSafeFinally$(SparkErrorUtils.scala:83)
E   	at org.apache.spark.util.Utils$.tryWithSafeFinally(Utils.scala:97)
E   	at org.apache.spark.executor.Executor$TaskRunner.run(Executor.scala:719)
E   	at java.base/java.util.concurrent.ThreadPoolExecutor.runWorker(ThreadPoolExecutor.java:1136)
E   	at java.base/java.util.concurrent.ThreadPoolExecutor$Worker.run(ThreadPoolExecutor.java:635)
E   	at java.base/java.lang.Thread.run(Thread.java:833)
----------------------------- Captured stderr call -----------------------------
26/05/09 12:26:11 WARN SparkContext: The path /Users/andrew/Desktop/andrew/Data_engineer/line_bot/finetune/spark_pipeline.py has been added already. Overwriting of added paths is not supported in the current version.
26/05/09 12:26:11 WARN TaskSetManager: Lost task 1.0 in stage 3.0 (TID 7) (192.168.1.101 executor driver): TaskKilled (Stage cancelled: Job aborted due to stage failure: Task 0 in stage 3.0 failed 1 times, most recent failure: Lost task 0.0 in stage 3.0 (TID 6) (192.168.1.101 executor driver): org.apache.spark.SparkException: 
Error from python worker:
  Traceback (most recent call last):
    File "/Library/Frameworks/Python.framework/Versions/3.8/lib/python3.8/runpy.py", line 185, in _run_module_as_main
      mod_name, mod_spec, code = _get_module_details(mod_name, _Error)
    File "/Library/Frameworks/Python.framework/Versions/3.8/lib/python3.8/runpy.py", line 111, in _get_module_details
      __import__(pkg_name)
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/__init__.py", line 53, in <module>
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/util.py", line 35, in <module>
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/__init__.py", line 21, in <module>
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/exceptions/base.py", line 23, in <module>
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/utils.py", line 40, in <module>
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/error_classes.py", line 26, in <module>
  AttributeError: module 'importlib.resources' has no attribute 'files'
PYTHONPATH was:
  /Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip:/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/py4j-0.10.9.9-src.zip:/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/jars/spark-core_2.13-4.1.1.jar
org.apache.spark.SparkException: EOFException occurred while reading the port number from pyspark.daemon's stdout and terminated with code: 1..
	at org.apache.spark.errors.SparkCoreErrors$.eofExceptionWhileReadPortNumberError(SparkCoreErrors.scala:55)
	at org.apache.spark.api.python.PythonWorkerFactory.startDaemon(PythonWorkerFactory.scala:339)
	at org.apache.spark.api.python.PythonWorkerFactory.createThroughDaemon(PythonWorkerFactory.scala:188)
	at org.apache.spark.api.python.PythonWorkerFactory.create(PythonWorkerFactory.scala:152)
	at org.apache.spark.SparkEnv.createPythonWorker(SparkEnv.scala:158)
	at org.apache.spark.api.python.BasePythonRunner.compute(PythonRunner.scala:309)
	at org.apache.spark.api.python.PythonRDD.compute(PythonRDD.scala:72)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.scheduler.ShuffleMapTask.runTask(ShuffleMapTask.scala:107)
	at org.apache.spark.scheduler.ShuffleMapTask.runTask(ShuffleMapTask.scala:54)
	at org.apache.spark.TaskContext.runTaskWithListeners(TaskContext.scala:180)
	at org.apache.spark.scheduler.Task.run(Task.scala:147)
	at org.apache.spark.executor.Executor$TaskRunner.$anonfun$run$5(Executor.scala:716)
	at org.apache.spark.util.SparkErrorUtils.tryWithSafeFinally(SparkErrorUtils.scala:86)
	at org.apache.spark.util.SparkErrorUtils.tryWithSafeFinally$(SparkErrorUtils.scala:83)
	at org.apache.spark.util.Utils$.tryWithSafeFinally(Utils.scala:97)
	at org.apache.spark.executor.Executor$TaskRunner.run(Executor.scala:719)
	at java.base/java.util.concurrent.ThreadPoolExecutor.runWorker(ThreadPoolExecutor.java:1136)
	at java.base/java.util.concurrent.ThreadPoolExecutor$Worker.run(ThreadPoolExecutor.java:635)
	at java.base/java.lang.Thread.run(Thread.java:833)

Driver stacktrace:)
26/05/09 12:26:11 ERROR Executor: Exception in task 1.0 in stage 4.0 (TID 9)
org.apache.spark.SparkException: 
Error from python worker:
  Traceback (most recent call last):
    File "/Library/Frameworks/Python.framework/Versions/3.8/lib/python3.8/runpy.py", line 185, in _run_module_as_main
      mod_name, mod_spec, code = _get_module_details(mod_name, _Error)
    File "/Library/Frameworks/Python.framework/Versions/3.8/lib/python3.8/runpy.py", line 111, in _get_module_details
      __import__(pkg_name)
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/__init__.py", line 53, in <module>
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/util.py", line 35, in <module>
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/__init__.py", line 21, in <module>
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/exceptions/base.py", line 23, in <module>
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/utils.py", line 40, in <module>
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/error_classes.py", line 26, in <module>
  AttributeError: module 'importlib.resources' has no attribute 'files'
PYTHONPATH was:
  /Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip:/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/py4j-0.10.9.9-src.zip:/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/jars/spark-core_2.13-4.1.1.jar
org.apache.spark.SparkException: EOFException occurred while reading the port number from pyspark.daemon's stdout and terminated with code: 1..
	at org.apache.spark.errors.SparkCoreErrors$.eofExceptionWhileReadPortNumberError(SparkCoreErrors.scala:55)
	at org.apache.spark.api.python.PythonWorkerFactory.startDaemon(PythonWorkerFactory.scala:339)
	at org.apache.spark.api.python.PythonWorkerFactory.createThroughDaemon(PythonWorkerFactory.scala:188)
	at org.apache.spark.api.python.PythonWorkerFactory.create(PythonWorkerFactory.scala:152)
	at org.apache.spark.SparkEnv.createPythonWorker(SparkEnv.scala:158)
	at org.apache.spark.api.python.BasePythonRunner.compute(PythonRunner.scala:309)
	at org.apache.spark.api.python.PythonRDD.compute(PythonRDD.scala:72)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.scheduler.ShuffleMapTask.runTask(ShuffleMapTask.scala:107)
	at org.apache.spark.scheduler.ShuffleMapTask.runTask(ShuffleMapTask.scala:54)
	at org.apache.spark.TaskContext.runTaskWithListeners(TaskContext.scala:180)
	at org.apache.spark.scheduler.Task.run(Task.scala:147)
	at org.apache.spark.executor.Executor$TaskRunner.$anonfun$run$5(Executor.scala:716)
	at org.apache.spark.util.SparkErrorUtils.tryWithSafeFinally(SparkErrorUtils.scala:86)
	at org.apache.spark.util.SparkErrorUtils.tryWithSafeFinally$(SparkErrorUtils.scala:83)
	at org.apache.spark.util.Utils$.tryWithSafeFinally(Utils.scala:97)
	at org.apache.spark.executor.Executor$TaskRunner.run(Executor.scala:719)
	at java.base/java.util.concurrent.ThreadPoolExecutor.runWorker(ThreadPoolExecutor.java:1136)
	at java.base/java.util.concurrent.ThreadPoolExecutor$Worker.run(ThreadPoolExecutor.java:635)
	at java.base/java.lang.Thread.run(Thread.java:833)
26/05/09 12:26:11 WARN TaskSetManager: Lost task 1.0 in stage 4.0 (TID 9) (192.168.1.101 executor driver): org.apache.spark.SparkException: 
Error from python worker:
  Traceback (most recent call last):
    File "/Library/Frameworks/Python.framework/Versions/3.8/lib/python3.8/runpy.py", line 185, in _run_module_as_main
      mod_name, mod_spec, code = _get_module_details(mod_name, _Error)
    File "/Library/Frameworks/Python.framework/Versions/3.8/lib/python3.8/runpy.py", line 111, in _get_module_details
      __import__(pkg_name)
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/__init__.py", line 53, in <module>
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/util.py", line 35, in <module>
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/__init__.py", line 21, in <module>
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/exceptions/base.py", line 23, in <module>
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/utils.py", line 40, in <module>
    File "<frozen importlib._bootstrap>", line 991, in _find_and_load
    File "<frozen importlib._bootstrap>", line 975, in _find_and_load_unlocked
    File "<frozen importlib._bootstrap>", line 655, in _load_unlocked
    File "<frozen importlib._bootstrap>", line 618, in _load_backward_compatible
    File "<frozen zipimport>", line 259, in load_module
    File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip/pyspark/errors/error_classes.py", line 26, in <module>
  AttributeError: module 'importlib.resources' has no attribute 'files'
PYTHONPATH was:
  /Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/pyspark.zip:/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/python/lib/py4j-0.10.9.9-src.zip:/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/jars/spark-core_2.13-4.1.1.jar
org.apache.spark.SparkException: EOFException occurred while reading the port number from pyspark.daemon's stdout and terminated with code: 1..
	at org.apache.spark.errors.SparkCoreErrors$.eofExceptionWhileReadPortNumberError(SparkCoreErrors.scala:55)
	at org.apache.spark.api.python.PythonWorkerFactory.startDaemon(PythonWorkerFactory.scala:339)
	at org.apache.spark.api.python.PythonWorkerFactory.createThroughDaemon(PythonWorkerFactory.scala:188)
	at org.apache.spark.api.python.PythonWorkerFactory.create(PythonWorkerFactory.scala:152)
	at org.apache.spark.SparkEnv.createPythonWorker(SparkEnv.scala:158)
	at org.apache.spark.api.python.BasePythonRunner.compute(PythonRunner.scala:309)
	at org.apache.spark.api.python.PythonRDD.compute(PythonRDD.scala:72)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.rdd.MapPartitionsRDD.compute(MapPartitionsRDD.scala:52)
	at org.apache.spark.rdd.RDD.computeOrReadCheckpoint(RDD.scala:374)
	at org.apache.spark.rdd.RDD.iterator(RDD.scala:338)
	at org.apache.spark.scheduler.ShuffleMapTask.runTask(ShuffleMapTask.scala:107)
	at org.apache.spark.scheduler.ShuffleMapTask.runTask(ShuffleMapTask.scala:54)
	at org.apache.spark.TaskContext.runTaskWithListeners(TaskContext.scala:180)
	at org.apache.spark.scheduler.Task.run(Task.scala:147)
	at org.apache.spark.executor.Executor$TaskRunner.$anonfun$run$5(Executor.scala:716)
	at org.apache.spark.util.SparkErrorUtils.tryWithSafeFinally(SparkErrorUtils.scala:86)
	at org.apache.spark.util.SparkErrorUtils.tryWithSafeFinally$(SparkErrorUtils.scala:83)
	at org.apache.spark.util.Utils$.tryWithSafeFinally(Utils.scala:97)
	at org.apache.spark.executor.Executor$TaskRunner.run(Executor.scala:719)
	at java.base/java.util.concurrent.ThreadPoolExecutor.runWorker(ThreadPoolExecutor.java:1136)
	at java.base/java.util.concurrent.ThreadPoolExecutor$Worker.run(ThreadPoolExecutor.java:635)
	at java.base/java.lang.Thread.run(Thread.java:833)

26/05/09 12:26:11 ERROR TaskSetManager: Task 1 in stage 4.0 failed 1 times; aborting job
=============================== warnings summary ===============================
.venv/lib/python3.13/site-packages/google/genai/types.py:9906
  /Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/google/genai/types.py:9906: PydanticDeprecatedSince212: Using `@model_validator` with mode='after' on a classmethod is deprecated. Instead, use an instance method. See the documentation at https://docs.pydantic.dev/2.13/concepts/validators/#model-after-validator. Deprecated in Pydantic V2.12 to be removed in V3.0.
    @model_validator(mode='after')  # type: ignore[arg-type]

main.py:2262: 1 warning
tests/test_organic_correction.py: 21 warnings
  /Users/andrew/Desktop/andrew/Data_engineer/line_bot/main.py:2262: DeprecationWarning: 
          on_event is deprecated, use lifespan event handlers instead.
  
          Read more about it in the
          [FastAPI docs for Lifespan Events](https://fastapi.tiangolo.com/advanced/events/).
          
    @app.on_event("startup")

.venv/lib/python3.13/site-packages/fastapi/applications.py:4598: 2 warnings
tests/test_organic_correction.py: 42 warnings
  /Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/fastapi/applications.py:4598: DeprecationWarning: 
          on_event is deprecated, use lifespan event handlers instead.
  
          Read more about it in the
          [FastAPI docs for Lifespan Events](https://fastapi.tiangolo.com/advanced/events/).
          
    return self.router.on_event(event_type)  # ty: ignore[deprecated]

main.py:2413: 1 warning
tests/test_organic_correction.py: 21 warnings
  /Users/andrew/Desktop/andrew/Data_engineer/line_bot/main.py:2413: DeprecationWarning: 
          on_event is deprecated, use lifespan event handlers instead.
  
          Read more about it in the
          [FastAPI docs for Lifespan Events](https://fastapi.tiangolo.com/advanced/events/).
          
    @app.on_event("startup")

.venv/lib/python3.13/site-packages/jieba/_compat.py:18
  /Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/jieba/_compat.py:18: UserWarning: pkg_resources is deprecated as an API. See https://setuptools.pypa.io/en/latest/pkg_resources.html. The pkg_resources package is slated for removal as early as 2025-11-30. Refrain from using this package or pin to Setuptools<81.
    import pkg_resources

tests/test_grounding_local.py::test_real_integration_canary_eps_uncertain
  <frozen importlib._bootstrap>:488: DeprecationWarning: builtin type SwigPyPacked has no __module__ attribute

tests/test_grounding_local.py::test_real_integration_canary_eps_uncertain
  <frozen importlib._bootstrap>:488: DeprecationWarning: builtin type SwigPyObject has no __module__ attribute

tests/test_spark_pipeline.py::test_enrich_adds_jieba_columns
tests/test_spark_pipeline.py::test_dedup_hash_stable
  /Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/pyspark/sql/udf.py:134: UserWarning: Cannot infer the eval type from type hints. 
    warnings.warn("Cannot infer the eval type from type hints. ", UserWarning)

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
=========================== short test summary info ============================
FAILED tests/test_auto_trigger_pilot.py::test_below_organic_threshold_does_not_trigger
FAILED tests/test_check_training_health.py::test_push_discord_no_webhook_url
FAILED tests/test_spark_pipeline.py::test_load_context_df - py4j.protocol.Py4...
FAILED tests/test_spark_pipeline.py::test_build_pairs_df_window_logic - py4j....
FAILED tests/test_spark_pipeline.py::test_full_pipeline_dedup_and_quality - p...
FAILED tests/test_spark_pipeline.py::test_enrich_adds_jieba_columns - py4j.pr...
FAILED tests/test_spark_pipeline.py::test_dedup_hash_stable - py4j.protocol.P...
7 failed, 753 passed, 94 warnings in 64.84s (0:01:04)
--- Logging error ---
Traceback (most recent call last):
  File "/opt/homebrew/Cellar/python@3.13/3.13.13/Frameworks/Python.framework/Versions/3.13/lib/python3.13/logging/__init__.py", line 1154, in emit
    stream.write(msg + self.terminator)
    ~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^
ValueError: I/O operation on closed file.
Call stack:
  File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/py4j/clientserver.py", line 673, in __del__
    self.close()
  File "/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/lib/python3.13/site-packages/py4j/clientserver.py", line 570, in close
    logger.info("Closing down clientserver connection")
Message: 'Closing down clientserver connection'
Arguments: ()
<sys>:0: DeprecationWarning: builtin type swigvarlink has no __module__ attribute
[12:27:13] pytest 失敗數: 0
[12:27:13] ## Step 3: pyflakes
```
/Users/andrew/Desktop/andrew/Data_engineer/line_bot/.venv/bin/python: No module named pyflakes
```
[12:27:14] pyflakes 警告: 0
0
[12:27:14] ## Step 4: 24h quality violations
```
找到 5 筆 correction notes（cols=['group_id', 'note_id', 'kind', 'scenario', 'content', 'created_at', 'source']）
- ('C83c5609ada4df93fa7f3239c24685133', 6, 'correction', '使用者糾正', '，你是我說了才記住，還是平常就會自己記住', 1778231254, 'rule_violation')
- ('C83c5609ada4df93fa7f3239c24685133', 5, 'correction', '使用者糾正', '....你會自動記住對吧？', 1778231219, 'rule_violation')
- ('C83c5609ada4df93fa7f3239c24685133', 4, 'correction', '使用者糾正', '那你不用投資了', 1777009886, 'rule_violation')
- ('C83c5609ada4df93fa7f3239c24685133', 3, 'correction', '影片/文章摘要', '影片或文章的摘要一律用條列（* 或數字）整理重點，不要寫成散文。每個重點用粗體標題開頭，例如「**核心論點**：...」，至少3~5條。', 1776845640, 'rule_violation')
- ('C83c5609ada4df93fa7f3239c24685133', 2, 'correction', '使用者糾正', '@All 紙盒裝食物，千萬不要放到微波爐去加熱。否則容出大量的塑膠微粒。能就已經顯示塑膠為例，傷害人體健康甚鉅。', 1776775595, 'rule_violation')
```
[12:27:14] ## Step 5: launchd_health / restart log tail
### line_bot_health_stderr.log (last 30)
```
cat: /Users/andrew/Desktop/andrew/Data_engineer/line_bot/pending_feedback.json: Operation not permitted
cat: /Users/andrew/Desktop/andrew/Data_engineer/line_bot/pending_feedback.json: Operation not permitted
```

### /tmp/line_bot_restart.log (last 30)
```
05-08 19:58:45 PT (10:58 TW) WARNING calendar_extractor | calendar extract failed (gemini-2.5-flash): 429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': 'You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.
[RAW] sig=Pp41+wvpMj0IqV69RP7mENqFMcIzJ+Y8ct9vEBv2CSg= len=931 body={"destination":"Ufb0f4b70bb1c5749ff4a45f7f743314a","events":[{"type":"message","message":{"type":"text","id":"613147196376219700","quoteToken":"zoj74kZ8o7QtWZ6wvJoqu58VLlCJS9iEcwrABouYpqzT9OAihyIOzSgvuySVTLvoV13bJdtJZSF61QNsS_EmcevSjBuIwojFvPiRq5vxPhvxjMRsamX8nMNxP7J60sNuHuajSp-jgaKCwNmwUrTHGw","markAsReadToken":"mBmYTnD2b27QyTbtZ-EHH1JhP9yIlg1YZUNRXsDAxkydKRT_1_54zExqmpp_D9iAbTdb5G000cSqxRtKw6BQuLTjurjK3tmizL2uERWMszbxUcAiwlcNxegYRp-WHe2iLppKGSPr1dfNK7-zLnkNprgK4mBND7lAAvQDtwzEEyDSjach8j_lKOtnhMY2Z-eNfmfkAjbq78KCCydwUIOPyw","text":"https://inline.app/reservations/-OrqX2eyCXRHSmeCq1b2?utm_source=line-oa-inline&utm_medium=push"},"webhookEventId":"01KR5AR7AW9P322382FS21VGG6","deliveryContext":{"isRedelivery":true},"timestamp":1778295512356,"source":{"type":"group","groupId":"C83c5609ada4df93
[PARSED] event_count=1
[EVENT] type=MessageEvent source=GroupSource group_id=C83c5609ada4df93fa7f3239c24685133
[EVENT_DUMP] (could not dump) repr=MessageEvent(type='message', source=GroupSource(type='group', group_id='C83c5609ada4df93fa7f3239c24685133', user_id='U3cd5b3c8ae4272b13d960a333705ac36'), timestamp=1778295512356, mode=<EventMode.ACTIVE: 'active'>, webhook_event_id='01KR5AR7AW9P322382FS21VGG6', delivery_context=DeliveryContext(is_redelivery=True), reply_token='acc46756a2a9491496d1b3fdc19553c5', message=TextMessageContent(type='text', id='613147196376219700', text='https://inline.app/reservations/-OrqX2eyCXRHSmeCq1b2?utm_source=line-oa-inline&utm_medium=push', emojis=None, mention=None, quote_token='zoj74kZ8o7QtWZ6wvJoqu58VLlCJS9iEcwrABouYpqzT9OAihyIOzSgvuySVTLvoV13bJdtJZSF61QNsS_EmcevSjBuIwojFvPiRq5vxPhvxjMRsamX8nMNxP7J60sNuHuajSp-jgaKCwNmwUrTHGw', quoted_message_id=None))
05-08 19:59:34 PT (10:59 TW) INFO line_bot | skip truly-duplicate redelivery msg_id=613147196376219700
INFO:     147.92.149.165:0 - "POST /callback HTTP/1.1" 200 OK
INFO:     127.0.0.1:56813 - "GET /health HTTP/1.1" 200 OK
INFO:     2001:b011:15:946c:2804:5977:5917:f129:0 - "GET /health HTTP/1.1" 200 OK
[RAW] sig=BoeSKvQ26inL5K1JUKRk3LHGAVdwZabbQW1S/Mxcoi8= len=887 body={"destination":"Ufb0f4b70bb1c5749ff4a45f7f743314a","events":[{"type":"message","message":{"type":"text","id":"613149436285812810","quoteToken":"8Z4xjfKeg4vcYZHPowsfB0QEvPh2ICjucbVCO83pVpOtRZ67A9p2Qe_R0ZbgKd1t0hx9RDib4MJAl_k6Gb55zomXwu60IbEV6h63leltp-Pw-S2gA5izEszTRJUCLT8bUoMLXTsjRVGoqgR9WdeM3w","markAsReadToken":"EHf5TJ2JJBaZ9Clx4xK_2pWFURmxLE4yB-yYleSOKUgAWxpGtBgjUocYnC7ddbaLayYiz94d-hx2sTSdMSCBg_skoHAUY9Phvki_ippK4Mv8nS3_gEEWltBYUEXG8fhJ7CiM5r3gxuVxCZiXPjDB4oUuYa_TEeSK9QmW5ldO7FERzqJKITmBBTJfNOM9lAlbkNm_AjcctiF6X9miDofCIw","text":"https://maps.app.goo.gl/VG9aR3huy1Rqv8XB9?g_st=il"},"webhookEventId":"01KR5C0ZGJV4FN6T1QE3PSBY47","deliveryContext":{"isRedelivery":false},"timestamp":1778296847561,"source":{"type":"group","groupId":"C83c5609ada4df93fa7f3239c24685133","userId":"U3cd5b3c8ae4272
[PARSED] event_count=1
[EVENT] type=MessageEvent source=GroupSource group_id=C83c5609ada4df93fa7f3239c24685133
[EVENT_DUMP] (could not dump) repr=MessageEvent(type='message', source=GroupSource(type='group', group_id='C83c5609ada4df93fa7f3239c24685133', user_id='U3cd5b3c8ae4272b13d960a333705ac36'), timestamp=1778296847561, mode=<EventMode.ACTIVE: 'active'>, webhook_event_id='01KR5C0ZGJV4FN6T1QE3PSBY47', delivery_context=DeliveryContext(is_redelivery=False), reply_token='935c35ee199e466baf30c8eeaecbf00f', message=TextMessageContent(type='text', id='613149436285812810', text='https://maps.app.goo.gl/VG9aR3huy1Rqv8XB9?g_st=il', emojis=None, mention=None, quote_token='8Z4xjfKeg4vcYZHPowsfB0QEvPh2ICjucbVCO83pVpOtRZ67A9p2Qe_R0ZbgKd1t0hx9RDib4MJAl_k6Gb55zomXwu60IbEV6h63leltp-Pw-S2gA5izEszTRJUCLT8bUoMLXTsjRVGoqgR9WdeM3w', quoted_message_id=None))
INFO:     147.92.149.165:0 - "POST /callback HTTP/1.1" 200 OK
05-08 20:20:56 PT (11:20 TW) INFO burst_filter | burst respond by heuristic (group=C83c5609ada4df93fa7f3239c24685133, text=https://maps.app.goo.gl/VG9aR3huy1Rqv8XB9?g_st=il)
05-08 20:20:56 PT (11:20 TW) INFO line_bot | burst flush triggered group=C83c5609ada4df93fa7f3239c24685133 text_len=49
05-08 20:20:57 PT (11:20 TW) INFO line_bot | prefetch OK url=https://maps.app.goo.gl/VG9aR3huy1Rqv8XB9?g_st=il chars=120
05-08 20:20:57 PT (11:20 TW) INFO google_genai.models | AFC is enabled with max remote calls: 10.
05-08 20:20:57 PT (11:20 TW) INFO httpx | HTTP Request: POST https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent "HTTP/1.1 429 Too Many Requests"
05-08 20:20:57 PT (11:20 TW) WARNING gemini_client | gemini main model 429 daily quota exhausted, falling back to gemini-2.5-flash-lite
05-08 20:20:57 PT (11:20 TW) INFO google_genai.models | AFC is enabled with max remote calls: 10.
05-08 20:20:58 PT (11:20 TW) INFO httpx | HTTP Request: POST https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent "HTTP/1.1 429 Too Many Requests"
05-08 20:20:58 PT (11:20 TW) WARNING line_bot | gemini quota marked exhausted until 2026-05-09 15:00 TW
05-08 20:20:58 PT (11:20 TW) WARNING line_bot | gemini chat (burst) quota exhausted
INFO:     127.0.0.1:61618 - "GET /health HTTP/1.1" 200 OK
INFO:     2001:b011:15:946c:2804:5977:5917:f129:0 - "GET /health HTTP/1.1" 200 OK
INFO:     Shutting down
INFO:     Waiting for application shutdown.
INFO:     Application shutdown complete.
INFO:     Finished server process [36358]
```
[12:27:14] ## ✅ 全綠，無需迭代
[12:27:14] ## Step 7: 仍有未 commit 變更，catch-all 上傳
