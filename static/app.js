document.addEventListener("DOMContentLoaded", () => {
  const list = document.querySelector("[data-mcq-list]");
  const addButton = document.querySelector("[data-add-mcq]");
  const template = document.querySelector("#mcq-template");

  if (list && addButton && template) {
    addButton.addEventListener("click", () => {
      list.append(template.content.cloneNode(true));
      list.lastElementChild?.querySelector("input")?.focus();
    });
    list.addEventListener("click", (event) => {
      if (event.target.closest("[data-remove-mcq]")) {
        event.target.closest(".mcq-row")?.remove();
      }
    });
  }

  const timer = document.querySelector("[data-countdown]");
  const quizForm = document.querySelector("[data-quiz-form]");
  if (timer && quizForm) {
    let seconds = Number(timer.dataset.countdown);
    const display = timer.querySelector("[data-timer-display]");
    const focusNotice = document.querySelector("[data-focus-notice]");
    let tabSwitches = Number(focusNotice?.dataset.tabSwitches || 0);
    let resumePosition = Number(quizForm.dataset.resumePosition || 0);
    let saveTimeout;
    let timerInterval;
    let saveInterval;
    let switchPending = false;

    const collectAnswers = () => {
      const answers = {};
      for (const [name, value] of new FormData(quizForm)) {
        if (name.startsWith("answer_")) {
          answers[name.slice("answer_".length)] = value;
        }
      }
      return answers;
    };

    const payload = () => ({ answers: collectAnswers(), position: resumePosition });

    const updateVisiblePosition = () => {
      const questions = [...quizForm.querySelectorAll("[data-question-index]")];
      const visible = questions.find((question) => {
        const bounds = question.getBoundingClientRect();
        return bounds.bottom > 120 && bounds.top < window.innerHeight;
      });
      if (visible) resumePosition = Number(visible.dataset.questionIndex);
    };

    const currentPosition = (target) => {
      const question = target?.closest?.("[data-question-index]");
      if (question) {
        resumePosition = Number(question.dataset.questionIndex);
      } else {
        updateVisiblePosition();
      }
      return resumePosition;
    };

    const saveProgress = () => {
      if (quizForm.dataset.finished === "true" || document.hidden) return;
      fetch(quizForm.dataset.saveUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload()),
        keepalive: true,
      }).catch(() => {});
    };

    const scheduleSave = (event) => {
      currentPosition(event.target);
      window.clearTimeout(saveTimeout);
      saveTimeout = window.setTimeout(saveProgress, 350);
    };

    quizForm.addEventListener("input", scheduleSave);
    quizForm.addEventListener("change", scheduleSave);
    quizForm.addEventListener("focusin", (event) => currentPosition(event.target));
    window.addEventListener("scroll", updateVisiblePosition, { passive: true });
    saveInterval = window.setInterval(saveProgress, 10000);

    const restoreQuestionPosition = () => {
      const question = quizForm.querySelector(`[data-question-index="${resumePosition}"]`);
      question?.scrollIntoView({ block: "start" });
    };
    window.requestAnimationFrame(restoreQuestionPosition);

    const tick = () => {
      const minutes = Math.floor(seconds / 60);
      const remainder = seconds % 60;
      display.textContent = `${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}`;
      timer.classList.toggle("timer-warning", seconds <= 300);
      if (seconds <= 0) {
        window.clearInterval(timerInterval);
        quizForm.dataset.autoSubmit = "true";
        quizForm.requestSubmit();
        return;
      }
      seconds -= 1;
    };
    tick();
    timerInterval = window.setInterval(tick, 1000);
    quizForm.addEventListener("submit", (event) => {
      if (quizForm.dataset.autoSubmit !== "true" && !window.confirm("Submit this quiz now? You cannot change answers afterward.")) {
        event.preventDefault();
      } else {
        quizForm.dataset.finished = "true";
        window.clearInterval(timerInterval);
        window.clearInterval(saveInterval);
      }
    });

    document.addEventListener("visibilitychange", () => {
      if (document.hidden) {
        if (switchPending || quizForm.dataset.finished === "true") return;
        switchPending = true;
        updateVisiblePosition();
        fetch(quizForm.dataset.switchUrl, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload()),
          keepalive: true,
        }).then((response) => response.json()).then((result) => {
          tabSwitches = Number(result.tab_switches ?? tabSwitches);
          if (result.status === "incomplete") {
            quizForm.dataset.finished = "true";
            window.location.assign(quizForm.dataset.resultUrl);
            return;
          }
          if (focusNotice) {
            focusNotice.textContent = `Tab switch ${tabSwitches} of 3 recorded. ${Math.max(0, 3 - tabSwitches)} warning(s) remain.`;
          }
        }).catch(() => {}).finally(() => {
          switchPending = false;
        });
        return;
      }
      fetch(quizForm.dataset.stateUrl)
        .then((response) => response.json())
        .then((state) => {
          tabSwitches = Number(state.tab_switches ?? tabSwitches);
          if (state.status === "incomplete") {
            quizForm.dataset.finished = "true";
            window.location.assign(quizForm.dataset.resultUrl);
          } else if (focusNotice && tabSwitches > 0) {
            focusNotice.textContent = `Tab switch ${tabSwitches} of 3 recorded. ${Math.max(0, 3 - tabSwitches)} warning(s) remain.`;
          }
        })
        .catch(() => {});
    });

    window.addEventListener("beforeunload", () => {
      if (quizForm.dataset.finished !== "true" && navigator.sendBeacon) {
        updateVisiblePosition();
        navigator.sendBeacon(
          quizForm.dataset.saveUrl,
          new Blob([JSON.stringify(payload())], { type: "application/json" }),
        );
      }
    });
  }
});