<template>
    <div class="speech-recorder">
        <button @click="toggleRecording">
            {{ isRecording ? '停止录音' : '开始录音' }}
        </button>
        <p v-if="transcription">识别结果：{{ transcription }}</p>
        <p v-if="error" class="error">错误：{{ error }}</p>
    </div>
</template>

<script setup>
import { ref } from 'vue'

const isRecording = ref(false)
const transcription = ref('')
const error = ref('')
let mediaRecorder = null
let audioChunks = []

const toggleRecording = async () => {
    error.value = ''
    transcription.value = ''

    if (isRecording.value) {
        stopRecording()
        return
    }

    try {
        const stream = await navigator.mediaDevices.getUserMedia({
            audio: true,
        })
        audioChunks = []
        mediaRecorder = new MediaRecorder(stream)

        mediaRecorder.ondataavailable = (event) => {
            if (event.data && event.data.size > 0) {
                audioChunks.push(event.data)
            }
        }

        mediaRecorder.onstop = async () => {
            const audioBlob = new Blob(audioChunks, { type: 'audio/webm' })
            const formData = new FormData()
            formData.append('file', audioBlob, 'recording.webm')

            try {
                const response = await fetch(
                    'http://localhost:8000/transcribe',
                    {
                        method: 'POST',
                        body: formData,
                    },
                )
                if (!response.ok) {
                    throw new Error(`服务端返回 ${response.status}`)
                }
                const result = await response.json()
                transcription.value = result.text || '未识别到内容'
            } catch (err) {
                error.value = err.message
            }
        }

        mediaRecorder.start()
        isRecording.value = true
    } catch (err) {
        error.value = err.message
    }
}

const stopRecording = () => {
    if (mediaRecorder && mediaRecorder.state !== 'inactive') {
        mediaRecorder.stop()
        isRecording.value = false
    }
}
</script>

<style scoped>
.speech-recorder {
    display: flex;
    flex-direction: column;
    gap: 12px;
}
.error {
    color: red;
}
</style>
