# Use an official lightweight Python image
FROM python:3.12-slim

# Set the working directory in the container
WORKDIR /app

# Copy dependencies & install them
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source files
COPY . .

# Expose ports 8000 (FastAPI) & 8001 (ADK Web UI)
EXPOSE 8000 8001

# Make the startup script executable
RUN chmod +x start.sh

# Run start.sh to launch both processes in parallel & then monitor.
# If either crashes, the script exits and Kubernetes restarts the pod.
CMD ["./start.sh"]

# Command to run the application
# CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]