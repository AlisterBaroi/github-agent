# Use an official lightweight Python image
FROM python:3.12-slim

# Set the working directory in the container
WORKDIR /app

# Copy dependencies & install them
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy your FastAPI agent code files
# COPY agent.py .
# COPY tools_catalogue.py .
# COPY main.py .
COPY . .


# Expose the port Uvicorn runs on
EXPOSE 8000

# Command to run the application
# CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
CMD ["adk", "web", "--host", "0.0.0.0", "--port", "8001"]