#ifndef LOOP_TASK_H
#define LOOP_TASK_H

#include <Arduino.h>
#include <vector>

// Lightweight one-shot periodic timer.
// Embed as a value member when a class needs multiple independent throttles.
//
// Usage:
//   Ticker safetyTick;
//   if (safetyTick.elapsed(MANUAL_SAFETY_POLL_MS)) { ... }
struct Ticker {
    // elapsed() returns true and resets the timer when at least intervalMs
    // milliseconds have passed since the last reset (or since construction).
    // On the very first call (lastMs == 0) it always fires so tasks start
    // promptly rather than waiting a full interval at boot.
    bool elapsed(unsigned long intervalMs) {
        unsigned long now = millis();
        if (lastMs != 0 && now - lastMs < intervalMs)
            return false;
        lastMs = now;
        return true;
    }

    // Force the next elapsed() call to return true regardless of interval.
    void reset() { lastMs = 0; }

    // True if the ticker has never fired.
    bool isNew() const { return lastMs == 0; }

private:
    unsigned long lastMs = 0;
};

// Abstract base for all manager classes that participate in the Arduino loop().
//
// Subclasses implement tick() — the throttled body that runs at most once per
// intervalMs. loop() is called every Arduino loop iteration and handles the
// timing gate; tick() is only called when the interval has elapsed.
//
// For managers that must run every iteration without a fixed outer throttle
// (e.g. NeatoSerial's UART state machine) pass intervalMs = 0. With zero
// interval, tick() is called every loop() invocation.
//
// For managers that need multiple independent sub-timers inside tick(), embed
// Ticker members directly in the subclass — see ManualCleanManager for an
// example of this pattern.
class LoopTask {
public:
    // intervalMs — minimum milliseconds between tick() invocations.
    //   0 = run every loop() call (no throttle).
    explicit LoopTask(unsigned long intervalMs = 0) : _intervalMs(intervalMs) {}

    virtual ~LoopTask() = default;

    // Call from Arduino loop(). Invokes tick() when the interval has elapsed.
    void loop() {
        if (_intervalMs == 0 || _ticker.elapsed(_intervalMs)) {
            tick();
        }
    }

    // Change the throttle interval at runtime (e.g. NotificationManager
    // switches between active/idle polling rates).
    void setInterval(unsigned long intervalMs) { _intervalMs = intervalMs; }

    unsigned long getInterval() const { return _intervalMs; }

protected:
    // Subclasses implement the actual work here.
    virtual void tick() = 0;

private:
    unsigned long _intervalMs;
    Ticker _ticker;
};

// Central registry for LoopTask instances that are called unconditionally
// every Arduino loop() iteration (after the OTA/firmware guard).
//
// Managers that have conditional dispatch (WiFiManager, FirmwareManager) are
// NOT registered here — they remain explicit calls in main.cpp so the
// WiFi-gating and OTA short-circuit logic is preserved.
//
// Usage:
//   In a manager constructor: TaskRegistry::add(this);
//   In main.cpp loop():       TaskRegistry::tickAll();
class TaskRegistry {
public:
    static void add(LoopTask *task) { tasks().push_back(task); }

    static void tickAll() {
        for (LoopTask *t: tasks()) {
            t->loop();
        }
    }

private:
    // Meyer's singleton — zero static-init-order issues, constructed on first use.
    static std::vector<LoopTask *>& tasks() {
        static std::vector<LoopTask *> registry;
        return registry;
    }
};

#endif // LOOP_TASK_H
