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
  });
}
