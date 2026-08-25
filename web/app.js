let csrfToken = "";
const byId = (id) => document.getElementById(id);
const notice = byId("notice");
const send = byId("send");
const dialog = byId("confirm");

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
    notice.textContent = "Could not load local status.";
  });

send.onclick = () => {
  const room = byId("room").value.trim();
  const text = byId("text").value;
  if (!room || !text.trim()) {
    notice.textContent = "Enter a room and a message.";
    return;
  }
  byId("confirm-room").textContent = room;
  dialog.showModal();
};

byId("cancel").onclick = () => dialog.close();

byId("confirm-send").onclick = async () => {
  dialog.close();
  send.disabled = true;
  notice.textContent = "Signing and sending…";
  try {
    const response = await fetch("/api/send", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
      body: JSON.stringify({ room: byId("room").value.trim(), text: byId("text").value }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Send failed.");
    const receipt = data.receipt ? ` Read-back receipt confirmed in the room (seq ${data.receipt.seq}).` : " The server accepted the message; a read-back receipt is not available yet.";
    notice.className = "notice success";
    notice.textContent = `✓ Message signed and accepted by the server (HTTP ${data.status}) in ${data.room}.${receipt}`;
    byId("text").value = "";
  } catch (error) {
    notice.textContent = error.message;
  } finally {
    send.disabled = false;
  }
};
