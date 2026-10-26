"""CI runner: report a thread dump if a platform-specific test deadlocks."""
import faulthandler
import sys
import unittest


class TimedResult(unittest.TextTestResult):
    def startTest(self, test):
        super().startTest(test)
        faulthandler.dump_traceback_later(90, exit=True)

    def stopTest(self, test):
        faulthandler.cancel_dump_traceback_later()
        super().stopTest(test)


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.discover("tests")
    result = unittest.TextTestRunner(verbosity=2, resultclass=TimedResult).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
