import 'dart:async';

import 'package:flutter_test/flutter_test.dart';
import 'package:klangk_frontend/utils/clipboard_flush.dart';

void main() {
  group('ClipboardFlushController', () {
    test('flush with nothing armed writes nothing and stays disarmed',
        () async {
      var writes = 0;
      final controller = ClipboardFlushController((_) async {
        writes++;
        return true;
      });

      await controller.flush();

      expect(writes, 0);
      expect(controller.isArmed, isFalse);
    });

    test('failed flush keeps the text armed for the next input event',
        () async {
      final written = <String>[];
      final controller = ClipboardFlushController((text) async {
        written.add(text);
        return false; // browser rejected the write again
      });

      controller.arm('selection');
      expect(controller.isArmed, isTrue);

      await controller.flush();
      await controller.flush();

      // Every input event retries the same pending text.
      expect(written, ['selection', 'selection']);
      expect(controller.isArmed, isTrue);
    });

    test('successful flush disarms the controller', () async {
      final written = <String>[];
      final controller = ClipboardFlushController((text) async {
        written.add(text);
        return true;
      });

      controller.arm('selection');
      await controller.flush();
      expect(written, ['selection']);
      expect(controller.isArmed, isFalse);

      // A later input event writes nothing.
      await controller.flush();
      expect(written, ['selection']);
    });

    test('a later arm replaces the pending text', () async {
      final written = <String>[];
      final controller = ClipboardFlushController((text) async {
        written.add(text);
        return true;
      });

      controller.arm('first selection');
      controller.arm('second selection');
      await controller.flush();

      // The newest selection wins; the older text never reaches the
      // clipboard.
      expect(written, ['second selection']);
      expect(controller.isArmed, isFalse);
    });

    test('an arm landing during an in-flight flush keeps the newer text',
        () async {
      final written = <String>[];
      late Completer<bool> firstWrite;
      var attempt = 0;
      final controller = ClipboardFlushController((text) async {
        written.add(text);
        attempt++;
        if (attempt == 1) {
          // The first write is still in flight when the second arm lands.
          firstWrite = Completer<bool>();
          return firstWrite.future;
        }
        return true;
      });

      controller.arm('older selection');
      final inFlight = controller.flush();
      controller.arm('newer selection');

      // The older write succeeds — it must not clear the newer pending text.
      firstWrite.complete(true);
      await inFlight;
      expect(controller.isArmed, isTrue);

      await controller.flush();
      expect(written, ['older selection', 'newer selection']);
      expect(controller.isArmed, isFalse);
    });

    test('clear drops the pending write without delivering it', () async {
      final written = <String>[];
      final controller = ClipboardFlushController((text) async {
        written.add(text);
        return true;
      });

      controller.arm('stale selection');
      controller.clear();
      expect(controller.isArmed, isFalse);

      await controller.flush();
      expect(written, isEmpty);
    });
  });
}
