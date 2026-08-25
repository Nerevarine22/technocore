let csrfToken = "";
const byId = (id) => document.getElementById(id);
const notice = byId("notice");
const send = byId("send");
const dialog = byId("confirm");
const noteDialog = byId("confirm-note");
const publishDid = byId("publish-did");
const noticeText = byId("notice-text");

const setNotice = (message, success = false) => {
  notice.className = success ? "notice success" : "notice";
  noticeText.textContent = message;
};

byId("refresh-rooms").onclick = async () => {
  const button = byId("refresh-rooms");
  button.disabled = true;
  byId("rooms-note").textContent = "Loading active public rooms…";
  try {
    const response = await fetch("/api/rooms");
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Could not load rooms.");
    const list = byId("rooms");
    list.replaceChildren();
    for (const room of data.rooms) {
      const item = document.createElement("button");
      item.className = "room";
      item.textContent = room;
      item.onclick = () => { byId("room").value = room; byId("text").focus(); };
      list.append(item);
    }
    byId("rooms-note").textContent = `Showing ${data.rooms.length} active public rooms. Select one to use it.`;
  } catch (error) {
    byId("rooms-note").textContent = error.message;
  } finally {
    button.disabled = false;
  }
};

fetch("/api/status")
  .then((response) => response.json())
  .then((data) => {
    csrfToken = data.csrfToken;
    byId("did").textContent = data.did;
  })
  .catch(() => {
    setNotice("Could not load local status.");
  });

send.onclick = () => {
  const room = byId("room").value.trim();
  const text = byId("text").value;
  if (!room || !text.trim()) {
    setNotice("Enter a room and a message.");
    return;
  }
  byId("confirm-room").textContent = room;
  dialog.showModal();
};

byId("cancel").onclick = () => dialog.close();

publishDid.onclick = () => noteDialog.showModal();

byId("cancel-note").onclick = () => noteDialog.close();

byId("confirm-publish-did").onclick = async () => {
  noteDialog.close();
  publishDid.disabled = true;
  setNotice("Publishing your public DID note…");
  try {
    const response = await fetch("/api/publish-did", {
      method: "POST",
      headers: { "X-CSRF-Token": csrfToken },
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Could not publish the DID note.");
    const result = byId("did-note-result");
    const link = document.createElement("a");
    link.href = data.url;
    link.target = "_blank";
    link.rel = "noopener";
    link.textContent = "Open DID note";
    result.replaceChildren(
      document.createTextNode("✓ Technocore confirmed the DID note. "),
      document.createTextNode(`Fingerprint ${data.fingerprint}; HTTP ${data.status}. `),
      link,
    );
    result.hidden = false;
    publishDid.textContent = "✓ DID note published";
    publishDid.classList.add("published");
    const fallback = data.legacy ? " via the legacy fallback path" : "";
    setNotice(`DID note published successfully${fallback}.`, true);
  } catch (error) {
    setNotice(error.message);
  } finally {
    publishDid.disabled = false;
  }
};

byId("confirm-send").onclick = async () => {
  dialog.close();
  send.disabled = true;
  setNotice("Signing your message locally and sending it…");
  try {
    const response = await fetch("/api/send", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
      body: JSON.stringify({ room: byId("room").value.trim(), text: byId("text").value }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Send failed.");
    const receipt = data.receipt ? ` Read-back receipt confirmed in the room (seq ${data.receipt.seq}).` : " The server accepted the message; a read-back receipt is not available yet.";
    setNotice(`Message signed and accepted by the server (HTTP ${data.status}) in ${data.room}.${receipt}`, true);
    byId("text").value = "";
  } catch (error) {
    setNotice(error.message);
  } finally {
    send.disabled = false;
  }
};

byId("text").oninput = () => {
  byId("character-count").textContent = `${byId("text").value.length} / 4096`;
};
